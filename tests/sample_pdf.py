"""Builds a small judgment-shaped PDF for the tests.

The text is invented. It only imitates the layout of a Supreme Court
judgment: a header, section headings, numbered paragraphs, a numbered list inside a
paragraph, statute references in several styles, case citations, and a
'Page X of Y' footer on every page.
"""

from __future__ import annotations

from pathlib import Path

HEADER = [
    "REPORTABLE",
    "IN THE SUPREME COURT OF INDIA",
    "CRIMINAL APPELLATE JURISDICTION",
    "CRIMINAL APPEAL NO. 999 OF 2019",
    "Sample Appellant ... Appellant",
    "VERSUS",
    "State of Example ... Respondent",
    "J U D G M E N T",
    "A.B. TESTER, J.",
]

PARAGRAPHS = [
    "1. Leave granted. This invented appeal is test data and is not a real judgment.",
    "FACTUAL BACKGROUND",
    "2. The appellant, a surgeon, was charged under Section 304A IPC after a patient died "
    "during an operation. The first information report was registered under Section 154 Cr.P.C. "
    "at the local police station on the complaint of the patient's son.",
    "3. The High Court declined to quash the proceedings under Section 482 of the Code of "
    "Criminal Procedure, 1973. The appellant is before us against that order.",
    "SUBMISSIONS",
    "4. Learned counsel for the appellant submitted, relying on Jacob Mathew v. State of Punjab, "
    "(2005) 6 SCC 1 : AIR 2005 SC 3180, that negligence in the criminal law must be gross. The test in "
    "Bolam v. Friern Hospital Management Committee [1957] 1 WLR 582 was approved there.",
    "ANALYSIS",
    "5. The principles that emerge may be stated thus:",
    "1. A simple lack of care gives rise to civil liability only.",
    "2. For an offence under Section 304-A of the Indian Penal Code, the negligence must be gross.",
    "3. An error of judgment is not negligence.",
    "6. Applying those principles, we find no material showing gross negligence. " + (
        "The medical record shows that the standard procedure was followed at each stage, that "
        "consent was taken, and that the complication which arose is a recognised risk of the "
        "operation. " * 12
    ),
    "7. Counsel for the State referred to Articles 14 and 21 of the Constitution and to "
    "Section 2(1)(d) of the Consumer Protection Act, 1986. Neither assists the prosecution here.",
    "8. We also note Suresh Gupta v. Govt. of NCT of Delhi, (2004) 6 SCC 422, which was "
    "affirmed in Jacob Mathew (2005) 6 SCC 1.",
    "9. The offence, if committed today, would fall under Section 106(1) of the Bharatiya Nyaya "
    "Sanhita, 2023. Nothing in this judgment turns on that.",
    "O R D E R",
    "10. The appeal is allowed. The proceedings under Section 3O4A IPC are quashed.",
    "11. Pending applications stand disposed of.",
    "New Delhi; March 14, 2019",
]


def build_sample_pdf(path: str | Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    path = Path(path)
    styles = getSampleStyleSheet()
    story = []
    for line in HEADER:
        story.append(Paragraph(line, styles["Normal"]))
    story.append(Spacer(1, 18))
    for text in PARAGRAPHS:
        story.append(Paragraph(text, styles["Normal"]))
        story.append(Spacer(1, 16))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(A4[0] / 2, 30, f"Page {doc.page} of 4")
        canvas.drawString(72, A4[1] - 40, "Crl. A. No. 999 of 2019")
        canvas.restoreState()

    SimpleDocTemplate(str(path), pagesize=A4, topMargin=90, bottomMargin=470).build(
        story, onFirstPage=footer, onLaterPages=footer
    )
    return path


if __name__ == "__main__":
    print(build_sample_pdf(Path(__file__).parent / "sample_judgment.pdf"))
