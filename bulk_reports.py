"""Well-organized PDF reports for bulk jobs. Password values are never included."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


EMAIL_STATUS_LABELS = {
    "en": {
        "uploaded": "Uploaded",
        "converted": "Converted",
        "conversion_failed": "Conversion failed",
        "check_failed": "Check failed",
        "duplicate": "Duplicate",
        "already_set": "Already set; unchanged",
        "pending": "Pending",
        "success": "Bound email changed",
        "email_failed": "Email change failed",
        "otp_timeout": "OTP timeout",
    },
    "zh": {
        "uploaded": "已上传",
        "converted": "已转换",
        "conversion_failed": "转换失败",
        "check_failed": "检查失败",
        "duplicate": "重复账号",
        "already_set": "已设置；未更改",
        "pending": "待处理",
        "success": "绑定邮箱已更改",
        "email_failed": "邮箱更改失败",
        "otp_timeout": "验证码超时",
    },
}

TWOFA_STATUS_LABELS = {
    "en": {
        "not_started": "Not started",
        "running": "Running",
        "success": "Password changed",
        "failed": "Change failed",
        "skipped": "Skipped; session unavailable",
    },
    "zh": {
        "not_started": "未开始",
        "running": "进行中",
        "success": "密码已更改",
        "failed": "更改失败",
        "skipped": "已跳过；会话不可用",
    },
}


def _phone(item) -> str:
    value = str(getattr(item, "phone_number", "") or "").strip()
    if not value:
        return "—"
    return value if value.startswith("+") else f"+{value}"


def _font_name(language: str) -> str:
    if language == "zh":
        font_name = "STSong-Light"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        return font_name
    return "Helvetica"


def _write_report(output: Path, *, title: str, summary: str, headers: list[str], rows: list[list[str]], widths: list[float], language: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    font = _font_name(language)
    title_style = ParagraphStyle(
        "ReportTitle",
        fontName=font,
        fontSize=16,
        leading=20,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "ReportBody",
        fontName=font,
        fontSize=8.5,
        leading=11,
        wordWrap="CJK" if language == "zh" else None,
    )
    header_style = ParagraphStyle(
        "ReportHeader",
        parent=body_style,
        textColor=colors.white,
        fontSize=9,
        leading=11,
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    generated_label = "生成时间" if language == "zh" else "Generated"
    story = [
        Paragraph(escape(title), title_style),
        Paragraph(escape(summary), body_style),
        Spacer(1, 4),
        Paragraph(f"{generated_label}: {generated}", body_style),
        Spacer(1, 10),
    ]
    data = [[Paragraph(escape(str(cell)), header_style) for cell in headers]]
    data.extend(
        [Paragraph(escape(str(cell)), body_style) for cell in row]
        for row in rows
    )
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#8A8A8A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EAF2F8")]),
            ]
        )
    )
    story.append(table)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        label = "页码" if language == "zh" else "Page"
        canvas.drawCentredString(landscape(A4)[0] / 2, 10 * mm, f"{label} {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(output),
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Telegram Session Tools",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def create_bound_email_report(items: list, output: Path, language: str = "en") -> None:
    success = sum(item.status == "success" for item in items)
    if language == "zh":
        title = "批量绑定邮箱报告"
        summary = f"账号总数：{len(items)}；本次成功更改绑定邮箱：{success}。仅成功行显示新绑定邮箱。"
        headers = ["序号", "电话号码", "新绑定邮箱", "状态"]
    else:
        title = "Bulk Bound Email Report"
        summary = f"Total accounts: {len(items)}; bound email changed in this run: {success}. New email is shown only for successful rows."
        headers = ["No.", "Phone Number", "New Bound Email", "Status"]
    labels = EMAIL_STATUS_LABELS[language]
    rows = [
        [
            str(item.index),
            _phone(item),
            item.target.email if item.status == "success" else "—",
            labels.get(item.status, item.status),
        ]
        for item in items
    ]
    _write_report(
        output,
        title=title,
        summary=summary,
        headers=headers,
        rows=rows,
        widths=[12 * mm, 43 * mm, 91 * mm, 59 * mm],
        language=language,
    )


def create_twofa_report(items: list, output: Path, language: str = "en") -> None:
    success = sum(item.twofa_status == "success" for item in items)
    failed = sum(item.twofa_status == "failed" for item in items)
    skipped = sum(item.twofa_status == "skipped" for item in items)
    if language == "zh":
        title = "批量两步验证密码更改报告"
        summary = f"账号总数：{len(items)}；成功：{success}；失败：{failed}；跳过：{skipped}。报告不包含任何旧密码或新密码。"
        headers = ["序号", "电话号码", "状态", "备注（不含密码）"]
    else:
        title = "Bulk 2FA Password Change Report"
        summary = f"Total accounts: {len(items)}; success: {success}; failed: {failed}; skipped: {skipped}. No old or new passwords are included."
        headers = ["No.", "Phone Number", "Status", "Detail (no passwords)"]
    labels = TWOFA_STATUS_LABELS[language]
    rows = [
        [
            str(item.index),
            _phone(item),
            labels.get(item.twofa_status, item.twofa_status),
            item.twofa_detail or "—",
        ]
        for item in items
    ]
    _write_report(
        output,
        title=title,
        summary=summary,
        headers=headers,
        rows=rows,
        widths=[12 * mm, 43 * mm, 49 * mm, 101 * mm],
        language=language,
    )
