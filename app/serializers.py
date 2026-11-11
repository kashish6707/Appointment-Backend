"""JSON shapes shared by several routes (camelCase, like the request bodies)."""

import uuid

from app.models import Booking, DoctorClinic, OpdSession, Patient, PatientCorrection


def iso(value):
    return value.isoformat() if value is not None else None


def correction_for(p: Patient, doctor_id: uuid.UUID) -> PatientCorrection | None:
    return next((c for c in p.corrections if c.doctor_id == doctor_id), None)


def patient_basic(p: Patient, correction: PatientCorrection | None = None) -> dict:
    """The patient's basic details, with one doctor's corrections applied when given."""
    c = correction
    return {
        "id": str(p.id),
        "name": c.name if c and c.name is not None else p.name,
        "phone": p.phone,
        "age": c.age if c and c.age is not None else p.age,
        "gender": c.gender if c and c.gender is not None else p.gender,
        "corrected": c is not None,
    }


def booking_item(b: Booking) -> dict:
    """A booking as its doctor's dashboard lists it (basic patient details, with that doctor's
    corrections)."""
    doctor_id = b.session.doctor_clinic.doctor_id
    return {
        "id": str(b.id),
        "sessionId": str(b.session_id),
        "doctorClinicId": str(b.session.doctor_clinic_id),
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
        "patient": patient_basic(b.patient, correction_for(b.patient, doctor_id)),
    }


def session_json(s: OpdSession) -> dict:
    return {
        "id": str(s.id),
        "doctorClinicId": str(s.doctor_clinic_id),
        "date": s.date.isoformat(),
        "status": s.status,
        "startedAt": iso(s.started_at),
        "closedAt": iso(s.closed_at),
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
