import os
import json
import base64
import logging
import asyncio
import gspread
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN_B2B"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SHEET_URL         = os.environ["GOOGLE_SHEET_URL_B2B"]
GOOGLE_CREDS      = json.loads(os.environ["GOOGLE_CREDS_JSON"])
AUTHORIZED_USERS  = [int(x) for x in os.environ.get("AUTHORIZED_USERS", "").split(",") if x.strip()]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Columnas hoja FACTURACIÓN:
# B=CLIENTE C=FECHA PEDIDO D=N°FACTURA E=MONTO F=FECHA ENTREGA
# G=VENCIMIENTO H=ESTADO I=FECHA COBRO J=MÉTODO K=NOTAS

def get_sheet():
    creds = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_url(SHEET_URL)

def get_context(ss):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    lines = []
    for row in rows[5:]:
        if len(row) >= 8 and row[1].strip():
            lines.append(f"{row[1]} | {row[3]} | S/{row[4]} | vence {row[6]} | {row[7]}")
    return "\n".join(lines) if lines else "Sin registros"

def get_clientes(ss):
    ws = ss.worksheet("CLIENTES")
    rows = ws.get_all_values()
    return [row[1].strip() for row in rows[5:] if len(row) > 1 and row[1].strip()]

def build_system_prompt():
    hoy = datetime.now().strftime("%d/%m/%Y")
    return f"""Sos el asistente B2B de PechuFree (Lima, Perú). Gestionás pedidos, facturas y cobros.

Hoy es {hoy}.

Respondé SIEMPRE con un JSON válido, sin texto extra, sin markdown:
{{
  "action": "ninguna" | "registrar_venta" | "registrar_pago" | "registrar_pago_total" | "consultar_saldo" | "listar_vencidos" | "listar_pendientes",
  "params": {{}},
  "response": "mensaje para el usuario"
}}

ACCIONES:

registrar_venta → cuando hay un pedido o factura nueva
  params: {{"cliente":"...", "numero":"E001-XXX o PEDIDO", "monto":0.00, "fecha_pedido":"DD/MM/YYYY", "vencimiento":"DD/MM/YYYY", "notas":"..."}}

registrar_pago → cuando un cliente pagó una factura específica
  params: {{"cliente":"...", "numero_factura":"...", "fecha_cobro":"DD/MM/YYYY", "metodo":"Transferencia/Yape/Efectivo/..."}}

registrar_pago_total → cuando el cliente pagó todo lo pendiente/vencido
  params: {{"cliente":"...", "fecha_cobro":"DD/MM/YYYY", "metodo":"..."}}

consultar_saldo → ver estado de cuenta de un cliente
  params: {{"cliente":"..."}}

listar_vencidos → ver facturas vencidas
  params: {{}}

listar_pendientes → ver todo lo por cobrar
  params: {{}}

ninguna → respuesta conversacional
  params: {{}}

Si el usuario manda foto de comprobante: extraé monto, fecha y método de pago.
Si el usuario manda foto de factura: extraé número, monto y fecha.
Si falta info para completar una acción, pedila en el campo "response"."""

async def call_claude(system, user_text, image_b64=None):
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
    content.append({"type": "text", "text": user_text})

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "system": system, "messages": [{"role": "user", "content": content}]}
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

def sheet_registrar_venta(ss, params):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    next_row = max(len(rows) + 1, 6)
    ws.update(f"B{next_row}:K{next_row}", [[
        params.get("cliente", ""),
        params.get("fecha_pedido", datetime.now().strftime("%d/%m/%Y")),
        params.get("numero", "PEDIDO"),
        float(params.get("monto", 0)),
        "",
        params.get("vencimiento", ""),
        "PENDIENTE",
        "", "",
        params.get("notas", "")
    ]])

def sheet_registrar_pago(ss, params):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    cliente = params.get("cliente", "").strip()
    numero  = params.get("numero_factura", "").strip()
    fecha   = params.get("fecha_cobro", datetime.now().strftime("%d/%m/%Y"))
    metodo  = params.get("metodo", "")

    for i, row in enumerate(rows):
        if i < 5: continue
        if len(row) < 8: continue
        if row[1].strip() != cliente: continue
        if numero and row[3].strip() != numero: continue
        if row[7].strip() in ["PENDIENTE", "VENCIDO"]:
            ws.update(f"H{i+1}:J{i+1}", [["COBRADO", fecha, metodo]])
            return True
    return False

def sheet_registrar_pago_total(ss, params):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    cliente = params.get("cliente", "").strip()
    fecha   = params.get("fecha_cobro", datetime.now().strftime("%d/%m/%Y"))
    metodo  = params.get("metodo", "")
    count   = 0

    for i, row in enumerate(rows):
        if i < 5: continue
        if len(row) < 8: continue
        if row[1].strip() != cliente: continue
        if row[7].strip() in ["PENDIENTE", "VENCIDO"]:
            ws.update(f"H{i+1}:J{i+1}", [["COBRADO", fecha, metodo]])
            count += 1
    return count

def build_saldo(ss, cliente):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    vencidos = []
    pendientes = []
    cobrado = 0.0

    for row in rows[5:]:
        if len(row) < 8 or row[1].strip() != cliente: continue
        try: monto = float(str(row[4]).replace(",", ""))
        except: monto = 0.0
        estado = row[7].strip()
        if estado == "COBRADO":
            cobrado += monto
        elif estado == "VENCIDO":
            vencidos.append(f"  ⚠️ {row[3]} — S/{monto:.2f} — venció {row[6]}")
        elif estado == "PENDIENTE":
            pendientes.append(f"  🔵 {row[3]} — S/{monto:.2f} — vence {row[6]}")

    total = sum(vencidos + pendientes and [float(r.split("S/")[1].split(" ")[0]) for r in vencidos + pendientes] or [0])
    resp = f"📊 *{cliente}*\n"
    resp += f"Deuda total: S/{sum([float(r.split('S/')[1].split(' ')[0]) for r in vencidos+pendientes] or [0]):.2f}\n"
    resp += f"Ya cobrado: S/{cobrado:.2f}\n"
    if vencidos:
        resp += f"\n🔴 Vencidas ({len(vencidos)}):\n" + "\n".join(vencidos)
    if pendientes:
        resp += f"\n🔵 Pendientes ({len(pendientes)}):\n" + "\n".join(pendientes)
    if not vencidos and not pendientes:
        resp += "\n✅ Sin deuda pendiente"
    return resp

def build_vencidos(ss):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    lines = ["⚠️ *Facturas VENCIDAS:*\n"]
    total = 0.0
    for row in rows[5:]:
        if len(row) < 8 or row[7].strip() != "VENCIDO": continue
        try: monto = float(str(row[4]).replace(",", ""))
        except: monto = 0.0
        total += monto
        lines.append(f"• {row[1]} — {row[3]} — S/{monto:.2f} — venció {row[6]}")
    lines.append(f"\n*Total vencido: S/{total:.2f}*")
    return "\n".join(lines)

def build_pendientes(ss):
    ws = ss.worksheet("FACTURACIÓN")
    rows = ws.get_all_values()
    lines = ["📋 *Por cobrar:*\n"]
    total = 0.0
    for row in rows[5:]:
        if len(row) < 8 or row[7].strip() not in ["PENDIENTE", "VENCIDO"]: continue
        try: monto = float(str(row[4]).replace(",", ""))
        except: monto = 0.0
        total += monto
        emoji = "⚠️" if row[7].strip() == "VENCIDO" else "🔵"
        lines.append(f"{emoji} {row[1]} — {row[3]} — S/{monto:.2f} — {row[6]}")
    lines.append(f"\n*Total: S/{total:.2f}*")
    return "\n".join(lines)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHORIZED_USERS and user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("⛔ No autorizado.")
        return

    await update.message.reply_text("⏳ Procesando...")

    try:
        ss = get_sheet()
        ctx = get_context(ss)
        clientes = get_clientes(ss)
        system = build_system_prompt()

        text_msg  = update.message.text or update.message.caption or ""
        image_b64 = None

        if update.message.photo:
            photo = update.message.photo[-1]
            file  = await context.bot.get_file(photo.file_id)
            fbytes = await file.download_as_bytearray()
            image_b64 = base64.b64encode(fbytes).decode("utf-8")
            if not text_msg:
                text_msg = "El usuario mandó una foto de comprobante de pago."

        if not text_msg and not image_b64:
            await update.message.reply_text("Mandame texto o una foto.")
            return

        user_prompt = f"""Clientes activos: {', '.join(clientes)}

Estado FACTURACIÓN:
{ctx}

Mensaje: {text_msg}"""

        raw = await call_claude(system, user_prompt, image_b64)

        try:
            result = json.loads(raw)
        except:
            await update.message.reply_text(raw)
            return

        action   = result.get("action", "ninguna")
        params   = result.get("params", {})
        response = result.get("response", "")

        if action == "registrar_venta":
            sheet_registrar_venta(ss, params)
            response = f"✅ Venta registrada:\n{params.get('cliente')} — {params.get('numero','PEDIDO')} — S/{params.get('monto',0)}\nVence: {params.get('vencimiento','')}"

        elif action == "registrar_pago":
            ok = sheet_registrar_pago(ss, params)
            if ok:
                response = f"✅ Pago registrado:\n{params.get('cliente')} — {params.get('numero_factura','')} — {params.get('metodo','')}"
            else:
                response = f"⚠️ No encontré la factura '{params.get('numero_factura','')}' de {params.get('cliente','')}. Verificá el número."

        elif action == "registrar_pago_total":
            count = sheet_registrar_pago_total(ss, params)
            response = f"✅ {count} facturas marcadas como COBRADAS para {params.get('cliente','')}."

        elif action == "consultar_saldo":
            response = build_saldo(ss, params.get("cliente", ""))

        elif action == "listar_vencidos":
            response = build_vencidos(ss)

        elif action == "listar_pendientes":
            response = build_pendientes(ss)

        await update.message.reply_text(response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *PechuFree B2B — Asistente*\n\n"
        "Hablame en lenguaje natural:\n"
        "• _La Sanahoria pagó la E001\\-153 por Yape_\n"
        "• _Nuevo pedido Club Gourmet S/450, vence en 15 días_\n"
        "• _¿Cuánto nos debe La Sanahoria?_\n"
        "• _Mostrame las facturas vencidas_\n"
        "• _La Sanahoria pagó todo por transferencia_\n\n"
        "También podés mandar 📸 foto de comprobante o factura.",
        parse_mode="Markdown"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    logger.info("Bot B2B iniciado")
    app.run_polling()

if __name__ == "__main__":
    main()
