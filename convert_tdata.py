import argparse
import asyncio
import json
import os
import re
import sys
import time
from getpass import getpass
from pathlib import Path

from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon.tl import functions, types


def normalize_phone_number(phone_number: str) -> str:
    """Validate an international number and return the official digits-only form."""
    normalized = re.sub(r"[\s().-]", "", phone_number.strip())
    if not re.fullmatch(r"\+[1-9]\d{6,14}", normalized):
        raise ValueError(
            "Use the new phone number in international format, for example +919876543210"
        )
    return normalized[1:]


def require_standard_sent_code(response) -> types.auth.SentCode:
    """Reject responses that need a flow this headless client cannot truthfully handle."""
    if isinstance(response, types.auth.SentCodePaymentRequired):
        raise ValueError(
            "Telegram requested a paid verification flow; no payment or phone change was attempted"
        )
    if not isinstance(response, types.auth.SentCode):
        raise ValueError(
            "Telegram did not return a standard verification-code response "
            f"({type(response).__name__})"
        )
    if isinstance(response.type, types.auth.SentCodeTypeFirebaseSms):
        raise ValueError(
            "Telegram selected Firebase SMS, which requires official Android "
            "attestation; this headless flow cannot handle it"
        )
    return response


def phone_code_details(sent_code: types.auth.SentCode) -> tuple[str, str | None, int | None]:
    """Return safe delivery metadata; never expose the code or phone_code_hash."""
    delivery = type(sent_code.type).__name__
    next_type = type(sent_code.next_type).__name__ if sent_code.next_type else None
    resend_delay = (sent_code.timeout or 60) if sent_code.next_type else None
    return delivery, next_type, resend_delay


async def convert(tdata_dir: Path, output: Path) -> None:
    """Convert an authorized Telegram Desktop tdata folder to Telethon."""
    if not tdata_dir.is_dir():
        raise SystemExit(f"tdata folder not found: {tdata_dir}")
    if output.exists():
        raise SystemExit(f"Output already exists; choose a new filename: {output}")

    if sys.stdin.isatty():
        passcode = getpass("Telegram Desktop local passcode (none = Enter): ") or None
    else:
        # Non-interactive automation can optionally provide it through the environment.
        passcode = os.environ.get("TDATA_LOCAL_PASSCODE") or None

    tdesk = TDesktop(str(tdata_dir), passcode=passcode)
    if not tdesk.isLoaded():
        raise SystemExit("No authorized account could be loaded from this tdata folder")

    output.parent.mkdir(parents=True, exist_ok=True)
    client = await tdesk.ToTelethon(
        session=str(output),
        flag=UseCurrentSession,
        api=API.TelegramDesktop,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "The converted session is not authorized; the old session may be revoked"
            )

        me = await client.get_me()
        print("CONVERSION_SUCCESS")
        print(f"SESSION_PATH={output.resolve()}")
        print(f"SESSION_SIZE={output.stat().st_size}")
        print(f"VERIFIED_USER_ID={me.id}")
        print(f"VERIFIED_FIRST_NAME={me.first_name or ''}")
    finally:
        await client.disconnect()


async def check_session(session_path: Path) -> dict:
    """
    Validate a Telethon session and read account email/2FA status.

    This is read-only: it does not change email, password, or recovery settings.
    It never returns or prints the actual email pattern.
    """
    if not session_path.is_file():
        raise FileNotFoundError(f"Telethon session file not found: {session_path}")

    client = TelegramClient(str(session_path), api=API.TelegramDesktop)
    try:
        await client.connect()
        authorized = bool(await client.is_user_authorized())
        result = {
            "session_authorized": authorized,
            "user_id": None,
            "first_name": None,
            "two_fa_password_set": None,
            "verified_login_email_set": None,
            "recovery_method_configured": None,
            "unconfirmed_recovery_email_pending": None,
        }

        if not authorized:
            return result

        me = await client.get_me()
        password_info = await client(functions.account.GetPasswordRequest())
        result.update(
            user_id=me.id,
            first_name=me.first_name or "",
            two_fa_password_set=bool(password_info.has_password),
            verified_login_email_set=bool(password_info.login_email_pattern),
            recovery_method_configured=bool(password_info.has_recovery),
            unconfirmed_recovery_email_pending=bool(
                password_info.email_unconfirmed_pattern
            ),
        )
        return result
    finally:
        await client.disconnect()


async def change_login_email(session_path: Path, new_email: str) -> None:
    """
    Change the verified login email through Telegram's confirmation flow.

    This requires an interactive terminal. The verification code is read with
    hidden input and is never printed, stored, or accepted as a CLI argument.
    """
    if not sys.stdin.isatty():
        raise SystemExit(
            "Login email change must be run interactively so the verification "
            "code can be entered securely."
        )
    if not session_path.is_file():
        raise SystemExit(f"Telethon session file not found: {session_path}")

    new_email = new_email.strip()
    local_part, separator, domain = new_email.partition("@")
    if not separator or not local_part or not domain or "@" in domain:
        raise SystemExit("Please provide a valid new email address")

    client = TelegramClient(str(session_path), api=API.TelegramDesktop)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("The session is revoked or no longer authorized")

        me = await client.get_me()
        print(f"ACCOUNT_VALIDATED user_id={me.id}")
        print("This will start changing the account's verified login email.")
        confirmation = input("Type CHANGE to continue, anything else to cancel: ").strip()
        if confirmation != "CHANGE":
            raise SystemExit("Cancelled; no email change request was sent")

        sent_code = await client(
            functions.account.SendVerifyEmailCodeRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                email=new_email,
            )
        )
        print(f"VERIFICATION_CODE_SENT code_length={sent_code.length}")
        print("Check the new email inbox for Telegram's verification code.")

        verification_code = getpass("Enter the email verification code: ").strip()
        if not verification_code:
            raise SystemExit("No verification code entered; email was not changed")

        await client(
            functions.account.VerifyEmailRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                verification=types.EmailVerificationCode(code=verification_code),
            )
        )
        verification_code = ""

        password_info = await client(functions.account.GetPasswordRequest())
        print("LOGIN_EMAIL_CHANGE_SUCCESS")
        print(f"VERIFIED_LOGIN_EMAIL_SET={bool(password_info.login_email_pattern)}")
        print("The verified email value/pattern was not printed or stored by this tool.")
    finally:
        await client.disconnect()


async def change_phone_number(
    session_path: Path,
    new_phone_number: str,
    api_profile: str = "desktop",
) -> None:
    """
    Change the account phone number through Telegram's verification flow.

    The phone code hash stays in memory only. The received phone code is read
    with hidden input and is never printed, stored, or accepted as a CLI argument.
    """
    if not sys.stdin.isatty():
        raise SystemExit(
            "Phone number change must be run interactively so the verification "
            "code can be entered securely."
        )
    if not session_path.is_file():
        raise SystemExit(f"Telethon session file not found: {session_path}")

    try:
        request_phone = normalize_phone_number(new_phone_number)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if api_profile == "android":
        api = API.TelegramAndroid
    elif api_profile == "desktop":
        api = API.TelegramDesktop
    else:
        raise SystemExit("API profile must be 'desktop' or 'android'")

    client = TelegramClient(str(session_path), api=api, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("The session is revoked or no longer authorized")

        me = await client.get_me()
        print(f"ACCOUNT_VALIDATED user_id={me.id}")
        print("This will start changing the account's phone number.")
        confirmation = input("Type CHANGE to continue, anything else to cancel: ").strip()
        if confirmation != "CHANGE":
            raise SystemExit("Cancelled; no phone-change code was requested")

        response = await client(
            functions.account.SendChangePhoneCodeRequest(
                phone_number=request_phone,
                settings=types.CodeSettings(),
            )
        )
        if isinstance(response, types.auth.SentCodeSuccess):
            updated_me = await client.get_me()
            if updated_me.phone == request_phone:
                print("PHONE_NUMBER_CHANGE_SUCCESS")
                print("UPDATED_PHONE_MATCHES_REQUESTED=True")
                return
            raise SystemExit(
                "Telegram returned immediate success but the account phone did not match; "
                "no further request was attempted"
            )
        sent_code = require_standard_sent_code(response)

        requested_at = time.monotonic()
        resend_count = 0
        while True:
            delivery, next_type, resend_delay = phone_code_details(sent_code)
            print(
                f"PHONE_VERIFICATION_CODE_SENT api_profile={api_profile} "
                f"delivery={delivery} "
                f"code_length={getattr(sent_code.type, 'length', None)} "
                f"next_type={next_type} "
                f"timeout={resend_delay}"
            )
            prompt = "Enter the phone verification code"
            if next_type:
                prompt += f", or RESEND after {resend_delay} seconds"
            verification_code = getpass(prompt + ": ").strip()

            if verification_code.upper() == "RESEND":
                if not next_type:
                    print("Telegram did not advertise a fallback for this challenge.")
                    continue
                if resend_count >= 1:
                    print("The safe one-resend limit has already been used.")
                    continue
                remaining = max(0, int(resend_delay - (time.monotonic() - requested_at)))
                if remaining:
                    print(f"Wait {remaining} more seconds before resending.")
                    continue
                response = await client(
                    functions.auth.ResendCodeRequest(
                        phone_number=request_phone,
                        phone_code_hash=sent_code.phone_code_hash,
                    )
                )
                sent_code = require_standard_sent_code(response)
                requested_at = time.monotonic()
                resend_count += 1
                continue
            break

        if not verification_code:
            raise SystemExit("No verification code entered; phone number was not changed")

        await client(
            functions.account.ChangePhoneRequest(
                phone_number=request_phone,
                phone_code_hash=sent_code.phone_code_hash,
                phone_code=verification_code,
            )
        )
        verification_code = ""

        updated_me = await client.get_me()
        expected_phone = request_phone
        print("PHONE_NUMBER_CHANGE_SUCCESS")
        print(f"UPDATED_PHONE_MATCHES_REQUESTED={updated_me.phone == expected_phone}")
        print(f"STILL_AUTHORIZED={bool(await client.is_user_authorized())}")
        print("The phone code and phone_code_hash were not printed or stored by this tool.")
    except ValueError as exc:
        raise SystemExit(str(exc))
    finally:
        await client.disconnect()


def print_session_status(result: dict) -> None:
    print(f"SESSION_AUTHORIZED={result['session_authorized']}")
    if not result["session_authorized"]:
        print("The session is revoked or no longer authorized by Telegram.")
        return

    print(f"VERIFIED_USER_ID={result['user_id']}")
    print(f"VERIFIED_FIRST_NAME={result['first_name']}")
    print(f"TWO_FA_PASSWORD_SET={result['two_fa_password_set']}")
    print(f"VERIFIED_LOGIN_EMAIL_SET={result['verified_login_email_set']}")
    print(f"RECOVERY_METHOD_CONFIGURED={result['recovery_method_configured']}")
    print(
        "UNCONFIRMED_RECOVERY_EMAIL_PENDING="
        f"{result['unconfirmed_recovery_email_pending']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert tdata to Telethon, check session status, or change the "
            "account's verified login email or phone number through Telegram's "
            "confirmation flows"
        )
    )
    parser.add_argument(
        "tdata_dir",
        nargs="?",
        type=Path,
        help="Path to the complete tdata folder (conversion mode)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="New .session output path (conversion mode)",
    )
    parser.add_argument(
        "--check-session",
        type=Path,
        metavar="SESSION_FILE",
        help="Validate an existing Telethon .session and check email/2FA status",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print checker output as JSON (use with --check-session)",
    )
    parser.add_argument(
        "--session",
        type=Path,
        metavar="SESSION_FILE",
        help="Authorized Telethon session to use with --change-login-email",
    )
    parser.add_argument(
        "--change-login-email",
        metavar="NEW_EMAIL",
        help="Start an interactive, verification-code-protected login email change",
    )
    parser.add_argument(
        "--change-phone-number",
        metavar="NEW_PHONE",
        help="Start an interactive, verification-code-protected phone number change",
    )
    parser.add_argument(
        "--api-profile",
        choices=("desktop", "android"),
        default="desktop",
        help="Static client API profile for --change-phone-number (default: desktop)",
    )
    args = parser.parse_args()

    if args.api_profile != "desktop" and not args.change_phone_number:
        parser.error("--api-profile is only supported with --change-phone-number")

    if args.change_login_email and args.change_phone_number:
        parser.error("Choose only one account-modification option at a time")

    modification_selected = bool(args.change_login_email or args.change_phone_number)
    if modification_selected:
        if args.check_session or args.tdata_dir or args.output or args.json:
            parser.error(
                "Account-modification options cannot be combined with conversion/checker "
                "options; use --session with exactly one modification option"
            )
        if not args.session:
            parser.error("Account modification requires --session SESSION_FILE")
    elif args.session:
        parser.error(
            "--session requires --change-login-email or --change-phone-number"
        )
    elif args.check_session:
        if args.tdata_dir or args.output:
            parser.error("--check-session cannot be combined with tdata_dir or --output")
    else:
        if args.json:
            parser.error("--json requires --check-session")
        if not args.tdata_dir or not args.output:
            parser.error("Conversion mode requires tdata_dir and -o/--output")

    return args


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.change_login_email:
        asyncio.run(
            change_login_email(arguments.session, arguments.change_login_email)
        )
    elif arguments.change_phone_number:
        asyncio.run(
            change_phone_number(
                arguments.session,
                arguments.change_phone_number,
                api_profile=arguments.api_profile,
            )
        )
    elif arguments.check_session:
        status = asyncio.run(check_session(arguments.check_session))
        if arguments.json:
            print(json.dumps(status, ensure_ascii=False))
        else:
            print_session_status(status)
    else:
        asyncio.run(convert(arguments.tdata_dir, arguments.output))
