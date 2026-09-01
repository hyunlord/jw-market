import json
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def main() -> None:
    output_path = sys.argv[1]
    rows = json.load(sys.stdin)
    pdfmetrics.registerFont(TTFont("PortalSample", "/System/Library/Fonts/Supplemental/Arial.ttf"))
    pdfmetrics.registerFont(TTFont("PortalSampleBold", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"))
    styles = getSampleStyleSheet()
    styles["Title"].fontName = "PortalSampleBold"
    styles["BodyText"].fontName = "PortalSample"
    document = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    story = [
        Paragraph("Portal PR7 export sample - structured table", styles["Title"]),
        Paragraph("[Source: Internal data mart] All 20 rows", styles["BodyText"]),
        Spacer(1, 6 * mm),
    ]
    table = Table([["Period", "Sales", "Share"], *rows], colWidths=[50 * mm, 50 * mm, 50 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111827")),
                ("FONTNAME", (0, 0), (-1, 0), "PortalSampleBold"),
                ("FONTNAME", (0, 1), (-1, -1), "PortalSample"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94A3B8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)
    document.build(story)


if __name__ == "__main__":
    main()
