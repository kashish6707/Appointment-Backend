"""Write docs/whatsapp-templates.md from app/notifications.py KINDS, so the templates submitted to
Meta always match what the app sends. Run: python -m scripts.whatsapp_templates"""

import re
from pathlib import Path

from app.notifications import KINDS

SAMPLES = {
    "BOOKED": ["Sunita Devi", "12", "Dr. Rajeev Anand", "Anand Ortho Care", "Thu 20 Nov", "10 am", "https://clinically.in/track/abc?code=xyz"],
    "OPD_STARTED": ["Dr. Rajeev Anand", "Anand Ortho Care", "12", "5", "https://clinically.in/track/abc?code=xyz"],
    "BREAK": ["Dr. Rajeev Anand", "15", "11:30 am", "12", "https://clinically.in/track/abc?code=xyz"],
    "GET_READY": ["12", "Dr. Rajeev Anand", "Anand Ortho Care", "20", "11:40 am", "https://clinically.in/track/abc?code=xyz"],
    "RUNNING_LATE": ["Dr. Rajeev Anand", "12", "12:10 pm", "https://clinically.in/track/abc?code=xyz"],
    "NEXT": ["12", "Dr. Rajeev Anand", "Anand Ortho Care"],
    "YOUR_TURN": ["12", "Dr. Rajeev Anand"],
    "CANCELLED": ["12", "Dr. Rajeev Anand", "Thu 20 Nov"],
    "OPD_CLOSED": ["Dr. Rajeev Anand", "Anand Ortho Care", "12"],
}


def meta(text: str) -> str:
    return re.sub(r"\{(\d)\}", r"{{\1}}", text)


def main() -> None:
    out = [
        "# WhatsApp templates",
        "",
        "Create each one in **WhatsApp Manager → Message templates → Create template**:",
        "- Category: **Utility**. Name: exactly as below. Add **two languages**: English and Hindi, same name.",
        "- Body: paste the text exactly (copy the `{{1}}` variables too). No header, footer or buttons.",
        "- Meta asks for a sample value for each variable: use the samples listed.",
        "",
        "Generated from `app/notifications.py` by `python -m scripts.whatsapp_templates`. Don't edit by hand.",
        "",
    ]
    for kind, k in KINDS.items():
        out += [
            f"## `{k.template}`",
            "",
            "**English (en)**",
            "```",
            meta(k.en),
            "```",
            "**Hindi (hi)**",
            "```",
            meta(k.hi),
            "```",
            "Samples: " + ", ".join(f"{{{{{i}}}}} = {v}" for i, v in enumerate(SAMPLES[kind], start=1)),
            "",
        ]
    Path("docs/whatsapp-templates.md").write_text("\n".join(out))
    print(f"Wrote {len(KINDS)} templates")


if __name__ == "__main__":
    main()
