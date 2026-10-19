"""Generate a filled-out CMS-1500 claim form as a PDF.

CMS-1500 has 33 numbered boxes. This fills the ones NEMT needs:
  Box 1  - Type of insurance (Medicaid/Medicare/other)
  Box 2  - Patient name
  Box 3  - Patient DOB + gender
  Box 5  - Patient address
  Box 11 - Insured's member id (usually the patient's Medicaid ID)
  Box 12 - Patient's signature / "SIGNATURE ON FILE"
  Box 17 - Referring provider (left blank for self-referred)
  Box 21 - Diagnosis codes (ICD-10)
  Box 24 - Service lines (date, place of service, CPT, charge)
  Box 28 - Total charge
  Box 33 - Billing provider info + NPI

Fields not filled (unused for NEMT) are left blank. The form is an 8.5" x 11"
PDF matching the official CMS-1500 layout.
"""

from __future__ import annotations

import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.modules.billing.models import Claim, ClaimLine, PayerType
from app.modules.billing.formats.edi import BillingProvider, Payer, Subscriber


# Layout: each box is (x_pt, y_pt, width_pt, height_pt) from the TOP-LEFT of the page
# The real CMS-1500 uses 100th-of-inch coordinates. We simplify by using inches directly
# and picking the label positions from the official form.
BOXES = {
    "1":    (0.0,   6.05,  2.0,   1.75),    # insurance type checkboxes (drawn)
    "2":    (2.55,  7.55,  4.0,   0.20),    # patient name
    "3":    (7.00,  7.55,  1.20,  0.20),    # patient DOB
    "3s":   (7.00,  7.35,  1.20,  0.18),    # gender
    "5":    (2.55,  7.05,  4.0,   0.20),    # patient address
    "6":    (2.55,  6.75,  4.0,   0.20),    # relationship to insured
    "11":   (5.55,  6.15,  1.70,  0.20),    # insured's policy group / member id
    "12":   (2.55,  5.55,  4.0,   0.20),    # patient signature (drawn text)
    "17":   (4.55,  4.95,  2.7,   0.20),    # referring provider (blank)
    "21a":  (2.55,  4.55,  1.2,   0.20),    # diagnosis 1
    "24a":  (0.0,   3.55,  1.0,   0.20),    # service date
    "24b":  (1.00,  3.55,  0.7,   0.20),    # place of service (2-digit code)
    "24d":  (2.40,  3.55,  1.4,   0.20),    # CPT / HCPCS
    "24f":  (3.80,  3.55,  0.8,   0.20),    # charges
    "28":   (5.90,  2.35,  1.5,   0.20),    # total charge
    "33":   (0.0,   0.55,  4.5,   0.85),    # billing provider block
}


def render_cms1500(
    claim: Claim,
    lines: list[ClaimLine],
    *,
    subscriber: Subscriber,
    payer: Payer,
    billing_provider: BillingProvider,
) -> bytes:
    """Return the PDF as bytes, suitable for streaming in an HTTP response."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter

    # Draw a faint "background" CMS-1500 to make it look right
    c.setFillColor(colors.white)
    c.rect(0, 0, w, h, stroke=0, fill=1)
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.5)
    for name, (x, y, bw, bh) in BOXES.items():
        c.rect(x * inch, h - (y + bh) * inch, bw * inch, bh * inch, stroke=1, fill=0)

    # Helpers
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 8)

    def put(box: str, text: str, size: int = 8) -> None:
        x, y, bw, bh = BOXES[box]
        c.setFont("Helvetica", size)
        # Trim to fit the box width (approx 5.5 chars per point-height of 8pt)
        c.drawString(x * inch + 2, h - (y + bh) * inch + 2, text[: int(bw * 14)])

    # ---- Box 1: insurance type ----
    c.setFont("Helvetica-Bold", 7)
    c.drawString(0.05 * inch, h - 6.30 * inch, "1.")
    c.setFont("Helvetica", 7)
    for i, (label, selected) in enumerate([
        ("MEDICARE", payer.payer_id == "06102"),
        ("MEDICAID", claim.payer_type == PayerType.MEDICAID),
        ("TRICARE", False),
        ("CHAMPVA", False),
        ("GROUP HEALTH PLAN", claim.payer_type == PayerType.PRIVATE_INSURANCE),
        ("FECA BLK LUNG", False),
        ("OTHER", claim.payer_type in (PayerType.FACILITY, PayerType.BROKER, PayerType.PATIENT)),
    ]):
        y = h - (6.30 + i * 0.28) * inch
        c.rect(0.15 * inch, y - 0.02 * inch, 0.10 * inch, 0.10 * inch, stroke=1, fill=0)
        if selected:
            c.setFont("Helvetica-Bold", 9)
            c.drawString(0.15 * inch, y - 0.02 * inch, "X")
            c.setFont("Helvetica", 7)
        c.drawString(0.30 * inch, y, label)

    # ---- Box 2: patient name ----
    put("2", f"{subscriber.last_name}, {subscriber.first_name}")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(2.55 * inch, h - 7.80 * inch, "2. PATIENT'S NAME")

    # ---- Box 3: patient DOB + gender ----
    put("3", subscriber.dob.strftime("%m/%d/%Y"))
    put("3s", subscriber.gender)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(7.00 * inch, h - 7.80 * inch, "3. DOB")
    c.drawString(7.00 * inch, h - 7.45 * inch, "SEX")

    # ---- Box 5: patient address ----
    put("5", f"{subscriber.address_line}, {subscriber.city}, {subscriber.state} {subscriber.zip_code}")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(2.55 * inch, h - 7.30 * inch, "5. PATIENT'S ADDRESS")

    # ---- Box 11: insured's member id ----
    put("11", subscriber.member_id)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(5.55 * inch, h - 6.40 * inch, "11. INSURED'S I.D. NUMBER")

    # ---- Box 12: signature (a check-box + text on file) ----
    c.setFont("Helvetica", 8)
    c.drawString(2.55 * inch, h - 5.78 * inch, "SIGNATURE ON FILE")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(2.55 * inch, h - 5.35 * inch, "12. PATIENT'S SIGNATURE")

    # ---- Box 21: primary diagnosis ----
    if claim.diagnosis_code:
        put("21a", claim.diagnosis_code)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(2.55 * inch, h - 4.80 * inch, "21. DIAGNOSIS OR NATURE OF ILLNESS")

    # ---- Box 24: service lines ----
    c.setFont("Helvetica-Bold", 7)
    c.drawString(0.05 * inch, h - 3.30 * inch, "24. SERVICE LINES")
    c.setFont("Helvetica-Bold", 6)
    for j, label in [(0, "A. DATE"), (1, "B. POS"), (2, "D. CPT"), (3, "F. CHARGE")]:
        c.drawString((0.05 + j * 1.20) * inch, h - 3.45 * inch, label)

    line_count = min(len(lines), 6)      # the form has 6 line slots
    for i, line in enumerate(lines[:line_count]):
        y = h - (3.70 + i * 0.22) * inch
        c.setFont("Helvetica", 8)
        c.drawString(0.05 * inch, y, line.service_date.strftime("%m/%d/%Y"))
        c.drawString(1.05 * inch, y, claim.place_of_service_code or "99")
        c.drawString(2.45 * inch, y, line.procedure_code)
        c.drawString(3.85 * inch, y, f"{line.charge_cents / 100:.2f}")

    # ---- Box 28: total charge ----
    c.setFont("Helvetica-Bold", 10)
    c.drawString(5.90 * inch, h - 2.55 * inch, f"{claim.billed_cents / 100:.2f}")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(5.90 * inch, h - 2.20 * inch, "28. TOTAL CHARGE")

    # ---- Box 33: billing provider ----
    c.setFont("Helvetica-Bold", 8)
    c.drawString(0.05 * inch, h - 0.90 * inch, billing_provider.name)
    c.setFont("Helvetica", 7)
    c.drawString(0.05 * inch, h - 1.05 * inch, billing_provider.address_line)
    c.drawString(0.05 * inch, h - 1.20 * inch,
                 f"{billing_provider.city}, {billing_provider.state} {billing_provider.zip_code}")
    c.drawString(0.05 * inch, h - 1.35 * inch, f"NPI: {billing_provider.npi}")
    c.drawString(0.05 * inch, h - 1.50 * inch, f"TAX ID: {billing_provider.tax_id}")
    c.setFont("Helvetica-Bold", 7)
    c.drawString(0.05 * inch, h - 0.65 * inch, "33. BILLING PROVIDER INFO & PH.#")

    # ---- Header/footer ----
    c.setFont("Helvetica-Bold", 12)
    c.drawString(2.5 * inch, h - 0.40 * inch, "HEALTH INSURANCE CLAIM FORM")
    c.setFont("Helvetica", 7)
    c.drawString(2.5 * inch, h - 0.55 * inch, "CMS-1500 (02/12) — generated by MediRoutes")
    c.drawRightString(8.0 * inch, h - 0.55 * inch,
                      f"Claim: {claim.claim_number}  Printed: {date.today().strftime('%m/%d/%Y')}")

    c.showPage()
    c.save()
    return buf.getvalue()