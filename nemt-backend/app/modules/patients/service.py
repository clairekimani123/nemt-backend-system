from datetime import date

from app.core.security import blind_index, decrypt_field, encrypt_field
from app.modules.patients.models import Patient
from app.modules.patients.schemas import PatientOut


def _age(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def to_out(p: Patient, sensitive: bool) -> PatientOut:
    """Minimum-necessary view: plaintext DOB / Medicaid ID / notes only for privileged roles."""
    dob = date.fromisoformat(decrypt_field(p.date_of_birth_enc))
    extra: dict = {"is_minor": _age(dob) < 18}
    if sensitive:
        extra |= {
            "date_of_birth": dob,
            "medicaid_id": decrypt_field(p.medicaid_id_enc) if p.medicaid_id_enc else None,
            "notes": decrypt_field(p.notes_enc) if p.notes_enc else None,
        }
    return PatientOut.model_validate(p).model_copy(update=extra)


def apply_fields(p: Patient, data: dict) -> None:
    """Route sensitive inputs through encryption; copy the rest verbatim."""
    data = dict(data)
    if (dob := data.pop("date_of_birth", None)) is not None:
        p.date_of_birth_enc = encrypt_field(dob.isoformat())
    if "medicaid_id" in data:
        mid = data.pop("medicaid_id")
        if mid:
            compact = "".join(ch for ch in mid if ch.isalnum()).upper()
            p.medicaid_id_enc, p.medicaid_id_hash = encrypt_field(mid), blind_index(mid)
            p.medicaid_last4 = compact[-4:]
        else:
            p.medicaid_id_enc = p.medicaid_id_hash = p.medicaid_last4 = None
    if "notes" in data:
        notes = data.pop("notes")
        p.notes_enc = encrypt_field(notes) if notes else None
    for key, value in data.items():
        setattr(p, key, value)