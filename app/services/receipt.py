"""Receipt for a paid registration: the data, an HTML page to print, and a PDF.

The registration UUID is the capability -- it is unguessable, and a receipt is
only ever built for a registration that is actually PAID.
"""
import base64
from datetime import datetime, timezone
from functools import cache
from io import BytesIO
from pathlib import Path

from app.config import settings
from app.models import Payment, Registration, RegistrationStatus

COMPANY = "GRADORRA PRIVATE LIMITED"
WEBSITE = "https://gpet.org.in"

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOGO = ASSETS / "logo.png"                 # header, 360px
WATERMARK = ASSETS / "logo_watermark.png"  # same mark, alpha already faded to ~7%


@cache
def _data_uri(path: Path) -> str:
    """Inline the image so a printed or saved receipt never loses its logo."""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


class ReceiptUnavailable(Exception):
    pass


def _receipt_id(number: str | None, registration: Registration) -> str:
    if number:
        serial = number.rsplit("/", 1)[-1]
        return f"GPL-{settings.ack_exam_code}-{serial}"
    return f"GPL-{settings.ack_exam_code}-{str(registration.id)[:8].upper()}"


def build(registration: Registration, payment: Payment | None = None) -> dict:
    if registration.status is not RegistrationStatus.PAID:
        raise ReceiptUnavailable("Receipt is available only after a successful payment")

    student = registration.student
    ack = registration.acknowledgement.number if registration.acknowledgement else None
    paid_at = registration.paid_at or datetime.now(timezone.utc)

    return {
        "company": COMPANY,
        "title": f"{settings.ack_exam_code[:4]} 2026 - Pre-Registration Official Receipt"
        if registration.phase.value == "PRE_LAUNCH"
        else f"{settings.ack_exam_code[:4]} 2026 - Registration Official Receipt",
        "receipt_id": _receipt_id(ack, registration),
        "acknowledgement_number": ack,
        "issued_at": paid_at.strftime("%d %b %Y, %I:%M %p"),
        "status": "SUCCESSFUL & SECURED",
        "student": {
            "name": student.full_name,
            "father_name": student.father_name or "-",
            "class": f"Class {registration.class_level}",
            "district": f"{student.district.name}, {student.district.state}",
            "mobile": f"+91 {student.mobile[:5]} {student.mobile[5:]} (Verified)",
            "address": student.address_line or "-",
        },
        "payment": {
            "description": f"{settings.ack_exam_code[:4]} 2026 "
            f"{'Pre-Registration' if registration.phase.value == 'PRE_LAUNCH' else 'Registration'} Fee",
            "amount": f"Rs {registration.fee_amount_paise / 100:.2f}",
            "status": "Paid Online (Secure Gateway)",
            "reference": (payment.razorpay_payment_id if payment else None) or "-",
        },
        "website": WEBSITE,
    }


# ---------------------------------------------------------------- HTML

def to_html(r: dict) -> str:
    rows = "".join(
        f'<tr><td class="k">{k}</td><td class="v">{v}</td></tr>'
        for k, v in [
            ("Student Name", r["student"]["name"]),
            ("Father's Name", r["student"]["father_name"]),
            ("Class", r["student"]["class"]),
            ("District", r["student"]["district"]),
            ("WhatsApp No.", r["student"]["mobile"]),
            ("Address", r["student"]["address"]),
        ]
    )
    pay = "".join(
        f'<tr><td class="k">{k}</td><td class="v">{v}</td></tr>'
        for k, v in [
            ("Description", r["payment"]["description"]),
            ("Amount Paid", r["payment"]["amount"]),
            ("Payment Status", r["payment"]["status"]),
            ("Transaction Ref", r["payment"]["reference"]),
        ]
    )
    ack_block = (
        f'<div class="ack"><div class="ack-label">Acknowledgement Number</div>'
        f'<div class="ack-no">{r["acknowledgement_number"]}</div></div>'
        if r["acknowledgement_number"]
        else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipt {r['receipt_id']}</title>
<style>
 :root{{--ink:#14161a;--muted:#5f6672;--line:#d9dde2;--brand:#e8681f;--ok:#0f7b3d}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:#f4f5f7;color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
 .sheet{{position:relative;overflow:hidden;max-width:720px;margin:24px auto;background:#fff;border:1px solid var(--line);padding:32px}}
 .wm{{position:absolute;left:50%;top:55%;width:62%;transform:translate(-50%,-50%);pointer-events:none;z-index:0}}
 .sheet>*:not(.wm){{position:relative;z-index:1}}
 .logo{{display:block;height:64px;margin:0 auto 10px}}
 .head{{text-align:center;border-bottom:2px solid var(--ink);padding-bottom:14px}}
 .company{{font-size:18px;font-weight:800;letter-spacing:.04em}}
 .doc{{font-size:13px;color:var(--muted);margin-top:4px}}
 .status{{display:inline-block;margin-top:14px;background:#e8f5ed;color:var(--ok);font-weight:700;font-size:12px;padding:5px 12px;border-radius:99px}}
 .meta{{display:flex;justify-content:space-between;gap:16px;font-size:12px;color:var(--muted);margin-top:14px;flex-wrap:wrap}}
 h2{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}}
 table{{width:100%;border-collapse:collapse}}
 td{{padding:6px 0;vertical-align:top}}
 td.k{{color:var(--muted);width:40%}}
 td.v{{font-weight:600}}
 .ack{{margin-top:18px;border:1px dashed var(--brand);border-radius:8px;padding:12px;text-align:center;background:#fdf3ec}}
 .ack-label{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
 .ack-no{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:20px;font-weight:800;margin-top:3px}}
 .foot{{margin-top:28px;padding-top:12px;border-top:2px solid var(--ink);text-align:center;font-size:11px;color:var(--muted)}}
 .bar{{max-width:720px;margin:0 auto 0;display:flex;gap:10px;justify-content:flex-end}}
 .bar a,.bar button{{font:inherit;font-weight:600;padding:9px 16px;border-radius:8px;border:1px solid var(--brand);background:#fff;color:var(--brand);cursor:pointer;text-decoration:none}}
 .bar button{{background:var(--brand);color:#fff}}
 @media print{{
   body{{background:#fff}}
   .bar{{display:none}}
   .sheet{{margin:0;border:0;padding:0;max-width:none}}
   @page{{margin:14mm}}
 }}
</style></head>
<body>
<div class="bar">
  <a href="./receipt.pdf">Download PDF</a>
  <button onclick="window.print()">Print</button>
</div>
<div class="sheet">
  <img class="wm" src="{_data_uri(WATERMARK)}" alt="">
  <div class="head">
    <img class="logo" src="{_data_uri(LOGO)}" alt="GPET">
    <div class="company">{r['company']}</div>
    <div class="doc">{r['title']}</div>
    <div class="status">&#10003; {r['status']}</div>
  </div>
  <div class="meta"><span>Receipt ID: <b>{r['receipt_id']}</b></span><span>{r['issued_at']}</span></div>
  {ack_block}
  <h2>Student details</h2><table>{rows}</table>
  <h2>Payment breakdown</h2><table>{pay}</table>
  <div class="foot">
    This is a computer-generated digital receipt and does not require a signature.<br>
    {r['company'].title()} &middot; {r['website']}
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------- PDF

def to_pdf(r: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Receipt {r['receipt_id']}", author=r["company"],
    )
    base = getSampleStyleSheet()
    muted = colors.HexColor("#5f6672")

    st_company = ParagraphStyle("c", parent=base["Title"], fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=2)
    st_doc = ParagraphStyle("d", parent=base["Normal"], fontSize=9.5, alignment=TA_CENTER, textColor=muted)
    st_ok = ParagraphStyle("s", parent=base["Normal"], fontSize=9.5, alignment=TA_CENTER,
                           textColor=colors.HexColor("#0f7b3d"), spaceBefore=6)
    st_h = ParagraphStyle("h", parent=base["Normal"], fontSize=8, textColor=muted,
                          spaceBefore=14, spaceAfter=4, leading=10)
    st_ack = ParagraphStyle("a", parent=base["Normal"], fontName="Courier-Bold", fontSize=15,
                            alignment=TA_CENTER, spaceBefore=4)
    st_small = ParagraphStyle("f", parent=base["Normal"], fontSize=8, alignment=TA_CENTER, textColor=muted, leading=11)

    def kv_table(pairs):
        t = Table([[Paragraph(k, ParagraphStyle("k", parent=base["Normal"], fontSize=9.5, textColor=muted)),
                    Paragraph(f"<b>{v}</b>", ParagraphStyle("v", parent=base["Normal"], fontSize=9.5))]
                   for k, v in pairs], colWidths=[62 * mm, None])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        return t

    s = r["student"]
    p = r["payment"]
    logo_h = 20 * mm
    with open(LOGO, "rb") as fh:
        from PIL import Image as PILImage
        lw, lh = PILImage.open(fh).size
    story = [
        Image(str(LOGO), width=logo_h * lw / lh, height=logo_h),
        Spacer(1, 4),
        Paragraph(r["company"], st_company),
        Paragraph(r["title"], st_doc),
        Paragraph("&#10003; STATUS: " + r["status"], st_ok),
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#14161a")),
        Spacer(1, 6),
        kv_table([("Receipt ID", r["receipt_id"]), ("Date & Time", r["issued_at"])]),
    ]

    if r["acknowledgement_number"]:
        story += [
            Spacer(1, 8),
            Paragraph("ACKNOWLEDGEMENT NUMBER", st_h),
            Paragraph(r["acknowledgement_number"], st_ack),
        ]

    story += [
        Paragraph("STUDENT DETAILS", st_h),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d9dde2")),
        Spacer(1, 4),
        kv_table([
            ("Student Name", s["name"]), ("Father's Name", s["father_name"]),
            ("Class", s["class"]), ("District", s["district"]),
            ("WhatsApp No.", s["mobile"]), ("Address", s["address"]),
        ]),
        Paragraph("PAYMENT BREAKDOWN", st_h),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d9dde2")),
        Spacer(1, 4),
        kv_table([
            ("Description", p["description"]), ("Amount Paid", p["amount"]),
            ("Payment Status", p["status"]), ("Transaction Ref", p["reference"]),
        ]),
        Spacer(1, 18),
        HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#14161a")),
        Spacer(1, 5),
        Paragraph("This is a computer-generated digital receipt and does not require a signature.", st_small),
        Paragraph(f"{r['company'].title()} &middot; {r['website']}", st_small),
    ]

    def watermark(canvas, _doc):
        page_w, page_h = A4
        w = 125 * mm
        canvas.saveState()
        # a w x w box centred on the page; the image keeps its shape inside it
        canvas.drawImage(str(WATERMARK), (page_w - w) / 2, (page_h - w) / 2 - 10 * mm,
                         width=w, height=w, preserveAspectRatio=True, mask="auto", anchor="c")
        canvas.restoreState()

    doc.build(story, onFirstPage=watermark, onLaterPages=watermark)
    return buf.getvalue()
