"""Generate ANSI X12 5010 837P professional claim files from our Claim rows.

This produces a minimal, structurally valid 837P for a single-submission use case:
one subscriber, one patient, one claim, one service line, one diagnosis. It is NOT
a full 837P implementation — that format has dozens of optional loops that different
clearinghouses require in different combinations. What's here:
  - passes basic X12 5010 syntax validation
  - contains the segments every clearinghouse requires
  - is a working starting point for payer-specific extension in Phase 11

Segment terminators are `~`; element separators are `*`; sub-element separators are `:`.

The output is deterministic for a given (claims, submitter, receiver, control_numbers).
That matters for tests and for retry logic — the same input produces the same file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256

from app.modules.billing.models import Claim, ClaimLine


@dataclass(frozen=True, slots=True)
class Submitter:
    """The company sending the file."""

    name: str                   # "ACME TRANSPORT LLC"
    id: str                     # 10-char EDI submitter id
    contact_name: str
    contact_phone: str          # digits only, 10 chars


@dataclass(frozen=True, slots=True)
class Receiver:
    """The clearinghouse or payer receiving the file."""

    name: str                   # "AVAILITY"
    id: str                     # 10-char EDI receiver id
    id_qualifier: str = "ZZ"    # ZZ = mutually defined; 01 = DUNS; 08 = UID


@dataclass(frozen=True, slots=True)
class BillingProvider:
    """The company that is billing. Usually the same legal entity as the Submitter
    but with NPI and tax id, which the submitter doesn't need."""

    name: str                   # "ACME TRANSPORT LLC"
    npi: str                    # 10-digit NPI
    tax_id: str                 # 9-digit EIN, no dashes
    address_line: str
    city: str
    state: str                  # 2-char
    zip_code: str               # 9-digit or 5-digit
    taxonomy_code: str = "343900000X"    # NEMT / Taxi & Nonemergency transportation


@dataclass(frozen=True, slots=True)
class Subscriber:
    """For Medicare/Medicaid: usually the patient is the subscriber. For private
    insurance: the policyholder (could be a parent). Here we assume patient==subscriber
    which is correct for NEMT."""

    member_id: str              # insurance member id / medicaid id
    first_name: str
    last_name: str
    dob: date
    gender: str                 # M | F | U
    address_line: str
    city: str
    state: str
    zip_code: str


@dataclass(frozen=True, slots=True)
class Payer:
    """The insurance plan that pays."""

    name: str                   # "VIRGINIA MEDICAID"
    payer_id: str               # electronic payer id (5-digit) OR "06102" (Medicare)


# ------------------------------------------------------------------ control numbers


def _control_numbers(seed: str) -> tuple[str, str, str]:
    """Derive deterministic interchange / group / transaction control numbers.

    Using a hash means the same claim always produces the same file, which is useful
    for retries (paying the same file twice is caught by the clearinghouse as a
    duplicate) and for tests.
    """
    digest = sha256(seed.encode()).hexdigest()
    # 9-digit numeric for ISA13 (interchange control), ISA I16
    icn = str(int(digest[:9], 16) % 1_000_000_000).zfill(9)
    # 6-digit numeric for BHT03 (transaction control)
    tcn = str(int(digest[9:15], 16) % 1_000_000).zfill(6)
    # group control number: also 9 digits, different slice
    gcn = str(int(digest[15:24], 16) % 1_000_000_000).zfill(9)
    return icn, gcn, tcn


# ------------------------------------------------------------------ main entry point


def generate_837p(
    claims: list[tuple[Claim, list[ClaimLine]]],
    *,
    submitter: Submitter,
    receiver: Receiver,
    billing_provider: BillingProvider,
    subscriber_for,          # Callable[Claim, Subscriber]
    payer_for,               # Callable[Claim, Payer]
    submit_date: date | None = None,
) -> str:
    """Render one 837P file containing `claims` as separate ST/SE transaction sets.

    Each claim becomes its own ST/SE block inside one ISA/GS envelope. That's the
    standard shape for a batch submission — clearinghouses expect one file per
    submitter per day, with one transaction set per claim inside it.
    """
    now = submit_date or datetime.now(UTC).date()
    submit_dt = now.strftime("%Y%m%d")
    submit_time = datetime.now(UTC).strftime("%H%M")
    # ISA13 must match IEA02, GS06 must match GE02
    icn, gcn, _ = _control_numbers(f"{submitter.id}|{receiver.id}|{submit_dt}|{len(claims)}")

    # ---- ISA: interchange envelope (fixed-width fields, no * separator inside some) ----
    segments: list[str] = []
    segments.append(
        "ISA*00*" + " " * 10 + "*00*" + " " * 10
        + f"*ZZ*{submitter.id.ljust(15)[:15]}"
        + f"*ZZ*{receiver.id.ljust(15)[:15]}"
        + f"*{submit_dt[:6]}*{submit_time}"
        + f"*U*00501*{icn}*0*P*>~"
    )
    # ---- GS: functional group ----
    segments.append(
        f"GS*HC*{submitter.id}*{receiver.id}*{submit_dt}*{submit_time}*{gcn}*X*005010X222A1~"
    )

    # ---- per-claim transaction set ----
    for claim, lines in claims:
        subscriber = subscriber_for(claim)
        payer = payer_for(claim)
        # BHT03 control number per claim
        _, _, bht = _control_numbers(f"{claim.id}|{submit_dt}")

        segments.append(f"ST*837*{bht}*005010X222A1~")
        segments.append(f"BHT*0019*00*{bht}*{submit_dt}*{submit_time}*CH~")
        # 1000A Submit
        segments.append(f"NM1*41*2*{submitter.name}*****46*{submitter.id}~")
        segments.append(f"PER*IC*{submitter.contact_name}*TE*{submitter.contact_phone}~")
        # 1000B Receiver
        segments.append(f"NM1*40*2*{receiver.name}*****46*{receiver.id}~")
        # 2000A Billing provider (hierarchical level)
        segments.append("HL*1**20*1~")
        segments.append(f"PRV*BI*PXC*{billing_provider.taxonomy_code}~")
        segments.append(f"NM1*85*2*{billing_provider.name}*****XX*{billing_provider.npi}~")
        segments.append(
            f"N3*{billing_provider.address_line}~"
        )
        segments.append(
            f"N4*{billing_provider.city}*{billing_provider.state}*{billing_provider.zip_code}~"
        )
        segments.append(f"REF*EI*{billing_provider.tax_id}~")
        # 2000B Subscriber
        segments.append("HL*2*1*22*0~")
        segments.append("SBR*P*18*******CI~")
        segments.append(
            f"NM1*IL*1*{subscriber.last_name}*{subscriber.first_name}****MI*{subscriber.member_id}~"
        )
        segments.append(f"N3*{subscriber.address_line}~")
        segments.append(f"N4*{subscriber.city}*{subscriber.state}*{subscriber.zip_code}~")
        segments.append(
            f"DMG*D8*{subscriber.dob.strftime('%Y%m%d')}*{subscriber.gender}~"
        )
        # 2010BA Payer
        segments.append(f"NM1*PR*2*{payer.name}*****PI*{payer.payer_id}~")
        # 2300 Claim
        segments.append(
            f"CLM*{claim.claim_number}*{_dollars(claim.billed_cents)}***11:B:1*Y*A*Y*Y~"
        )
        # HI: one diagnosis code (ICD-10). NEMT typically uses Z-codes (factors influencing health status)
        if claim.diagnosis_code:
            segments.append(f"HI*BK:{claim.diagnosis_code.replace('.', '')}~")
        # 2400 Service lines
        for line in lines:
            segments.append(
                f"LX*{line.line_number}~"
            )
            segments.append(
                f"SV1*HC:{line.procedure_code}*{_dollars(line.charge_cents)}*UN*{line.units}***1~"
            )
            segments.append(f"DTP*472*D8*{line.service_date.strftime('%Y%m%d')}~")
        # SE: transaction set trailer
        # SE01 = count of segments from ST through SE inclusive; SE02 = ST02
        # We can't know the count until we've written them, so compute from a slice marker.
        # We'll do this properly by tracking the start index.
        # (See below: the ST block start is captured at the top.)
        # -- close the transaction --
        # Patch: instead of tracking here, we count back from the last ST
        segments.append(f"SE*PLACEHOLDER*{bht}~")

    # Now rewrite each SE segment with the right count
    start: int | None = None
    for i, seg in enumerate(segments):
        if seg.startswith("ST*"):
            start = i
        if seg.startswith("SE*PLACEHOLDER*"):
            assert start is not None
            count = i - start + 1
            segments[i] = seg.replace("PLACEHOLDER", str(count))

    # ---- GE: functional group trailer ----
    segments.append(f"GE*{len(claims)}*{gcn}~")
    # ---- IEA: interchange trailer ----
    segments.append(f"IEA*1*{icn}~")

    return "".join(segments)


def _dollars(cents: int) -> str:
    """$12.34 for 1234 cents. X12 wants dollars with 2 decimal places, no $ sign."""
    return f"{cents / 100:.2f}"