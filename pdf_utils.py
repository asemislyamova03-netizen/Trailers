# pdf_utils.py
from io import BytesIO
from datetime import date

def build_contract_pdf_bytes(ctx) -> bytes:
    """
    Локальный PDF без WeasyPrint: генерим через ReportLab.
    Для SIGEX достаточно валидного PDF-байтов.
    На AWS можно заменить на WeasyPrint.
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    contract = ctx.get("contract")
    customer = ctx.get("customer")
    trailer = ctx.get("trailer")
    item = ctx.get("item")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    y = h - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, f"ДОГОВОР № {getattr(contract, 'contract_number', None) or getattr(contract, 'id', '')}")
    y -= 25

    c.setFont("Helvetica", 11)
    cd = getattr(contract, 'contract_date', None)
    c.drawString(50, y, f"Дата: {cd.strftime('%Y-%m-%d') if cd else ''}")
    y -= 18

    c.drawString(50, y, f"Покупатель: {getattr(customer, 'name', '') if customer else ''}")
    y -= 18

    c.drawString(50, y, f"Прицеп VIN: {getattr(trailer, 'vin', '') if trailer else ''}")
    y -= 18

    c.drawString(50, y, f"Артикул: {getattr(item, 'article', '') if item else ''}")
    y -= 18

    price = getattr(contract, 'price', None)
    c.drawString(50, y, f"Сумма: {price if price is not None else ''}")
    y -= 18

    c.drawString(50, y, "Подпись будет выполнена в SIGEX.")
    c.showPage()
    c.save()

    return buf.getvalue()
