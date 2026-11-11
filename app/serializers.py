"""JSON shapes shared by several routes (camelCase, like the request bodies)."""

from app.models import Booking, DoctorClinic, OpdSession, Patient


def iso(value):
    return value.isoformat() if value is not None else None


def patient_basic(p: Patient) -> dict:
    return {"id": str(p.id), "name": p.name, "phone": p.phone, "age": p.age, "gender": p.gender}


def booking_item(b: Booking) -> dict:
    """A booking as the doctor's dashboard lists it (basic patient details only)."""
    return {
        "id": str(b.id),
        "sessionId": str(b.session_id),
        "token": b.token,
        "status": b.status,
        "visitType": b.visit_type,
        "source": b.source,
        "bookedAt": iso(b.booked_at),
        "calledAt": iso(b.called_at),
        "completedAt": iso(b.completed_at),
        "cancelledAt": iso(b.cancelled_at),
        "cancelledBy": b.cancelled_by,
        "cancelReason": b.cancel_reason,
        "patient": patient_basic(b.patient),
    }


def session_json(s: OpdSession) -> dict:
    return {
        "id": str(s.id),
        "doctorClinicId": str(s.doctor_clinic_id),
        "date": s.date.isoformat(),
        "status": s.status,
        "startedAt": iso(s.started_at),
        "currentToken": s.current_token,
    }


def clinic_slot(dc: DoctorClinic) -> dict:
    return {
        "doctorClinicId": str(dc.id),
        "clinicId": str(dc.clinic.id),
        "name": dc.clinic.name,
        "area": dc.clinic.area,
        "address": dc.clinic.address,
        "phone": dc.clinic.phone,
        "fee": dc.fee,
        "tokenAmount": dc.token_amount,
        "opdDays": dc.opd_days,
        "opdStart": dc.opd_start.strftime("%H:%M"),
        "opdEnd": dc.opd_end.strftime("%H:%M"),
    }
