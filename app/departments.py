"""The fixed list of departments patients browse by, and Patna's areas.

A doctor's free-text specialisation ("Orthopaedics", "MS Ortho", "Child specialist") is matched
to a department key when it's saved, so the home page can group and filter doctors.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Department:
    key: str
    name_en: str
    name_hi: str
    icon_key: str
    words: tuple[str, ...]  # lower-case word starts that identify it in free text

    def json(self) -> dict:
        return {"key": self.key, "nameEn": self.name_en, "nameHi": self.name_hi, "iconKey": self.icon_key}


DEPARTMENTS: tuple[Department, ...] = (
    Department("general_medicine", "General physician", "सामान्य चिकित्सक", "stethoscope", ("general physician", "general medicine", "physician", "medicine", "family")),
    Department("orthopaedics", "Orthopaedics", "हड्डी रोग", "bone", ("ortho", "bone", "joint", "spine")),
    Department("paediatrics", "Paediatrics", "शिशु रोग", "baby", ("paed", "pediat", "child", "neonat")),
    Department("gynaecology", "Gynaecology", "स्त्री रोग", "female", ("gyn", "obstet", "obg", "women")),
    Department("cardiology", "Cardiology", "हृदय रोग", "heart", ("cardio", "heart")),
    Department("dermatology", "Dermatology", "त्वचा रोग", "skin", ("derma", "skin")),
    Department("ent", "ENT", "नाक, कान, गला", "ear", ("ent", "otorhino", "ear")),
    Department("ophthalmology", "Eye", "नेत्र रोग", "eye", ("ophthal", "eye")),
    Department("neurology", "Neurology", "तंत्रिका रोग", "brain", ("neuro",)),
    Department("psychiatry", "Psychiatry", "मनोरोग", "mind", ("psych", "mental")),
    Department("general_surgery", "General surgery", "सामान्य शल्य चिकित्सा", "scalpel", ("general surg", "surgeon", "surgery")),
    Department("urology", "Urology", "मूत्र रोग", "kidney", ("uro",)),
    Department("gastroenterology", "Gastroenterology", "पेट रोग", "stomach", ("gastro", "stomach", "liver")),
    Department("dentistry", "Dental", "दंत रोग", "tooth", ("dent", "teeth", "tooth")),
    Department("oncology", "Oncology", "कैंसर रोग", "ribbon", ("onco", "cancer")),
    Department("pulmonology", "Chest & lungs", "छाती व फेफड़े", "lungs", ("pulmo", "chest", "lung", "respirat")),
    Department("endocrinology", "Diabetes & hormones", "मधुमेह व हार्मोन", "drop", ("endocr", "diabet", "thyroid", "hormone")),
)
BY_KEY = {d.key: d for d in DEPARTMENTS}


def match(text: str | None) -> str | None:
    """The department key for a free-text specialisation, or None if nothing fits.

    Specific departments are tried before the catch-all general ones, so "Orthopaedic surgeon"
    is Orthopaedics, not General surgery.
    """
    if not text:
        return None
    words = re.sub(r"[^a-z ]+", " ", text.lower())
    padded = f" {' '.join(words.split())}"
    general = ("general_medicine", "general_surgery")
    for d in sorted(DEPARTMENTS, key=lambda d: d.key in general):
        if any(f" {w}" in padded for w in d.words):
            return d.key
    return None


def name(key: str | None) -> str | None:
    return BY_KEY[key].name_en if key in BY_KEY else None


# Patna's areas: used when a patient picks an area instead of sharing their location, and as a
# clinic's position until its exact location is set. (Approximate centre points.)
AREAS: tuple[tuple[str, str, str, float, float], ...] = (
    ("gandhi_maidan", "Gandhi Maidan", "गांधी मैदान", 25.6195, 85.1416),
    ("boring_road", "Boring Road", "बोरिंग रोड", 25.6153, 85.1124),
    ("kankarbagh", "Kankarbagh", "कंकड़बाग", 25.5990, 85.1600),
    ("patliputra", "Patliputra Colony", "पाटलिपुत्र कॉलोनी", 25.6266, 85.1006),
    ("rajendra_nagar", "Rajendra Nagar", "राजेंद्र नगर", 25.6046, 85.1596),
    ("bailey_road", "Bailey Road", "बेली रोड", 25.6080, 85.0900),
    ("ashok_rajpath", "Ashok Rajpath", "अशोक राजपथ", 25.6200, 85.1700),
    ("fraser_road", "Fraser Road", "फ्रेज़र रोड", 25.6110, 85.1380),
    ("raja_bazar", "Raja Bazar", "राजा बाज़ार", 25.6040, 85.0960),
    ("danapur", "Danapur", "दानापुर", 25.6200, 85.0450),
    ("patna_city", "Patna City", "पटना सिटी", 25.5960, 85.2280),
)
# Where distances are measured from when the patient gives neither a location nor an area.
CITY_CENTRE = (25.6195, 85.1416)
