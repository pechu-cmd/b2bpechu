import logging
import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN_B2B")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")
SHEET_URL        = os.environ.get("GOOGLE_SHEET_URL_B2B")
GOOGLE_CREDS     = os.environ.get("GOOGLE_CREDS_JSON")
AUTHORIZED       = os.environ.get("AUTHORIZED_USERS", "").split(",")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Estados conversación
(
    FACT_CLIENTE, FACT_MONTO, FACT_VENCIMIENTO, FACT_NUMERO, FACT_DETALLE,
    PAGO_CONFIRMAR, PAGO_MANUAL_CLIENTE, PAGO_MANUAL_MONTO,
    PEDIDO_CLIENTE, PEDIDO_DETALLE, PEDIDO_MONTO, PEDIDO_ENTREGA,
) = range(12)

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_url(SHEET_URL)

def get_clientes():
    ws = get_sheet().worksheet("👥 CLIENTES")
    rows = ws.get_all_values()[5:]  # desde fila 6
    return [r[0] for r in rows if r and r[0] and r[0] != "← Agregar cliente"]

def get_cobranzas_pendientes():
    ws = get_sheet().worksheet("💳 COBRANZAS")
    rows = ws.get_all_values()[5:]
    resultado = []
    for i, r in enumerate(rows, start=6):
        if len(r) >= 6 and r[0] and r[5] in ["PENDIENTE", "VENCIDO"]:
            try:
                monto = float(str(r[3]).replace(",","").replace("S/","").strip())
                resultado.append({
                    "row": i, "cliente": r[0], "pedido": r[1],
                    "monto": monto, "vencimiento": r[4], "estado": r[5]
                })
            except: pass
    return resultado

def get_siguiente_numero_pedido():
    ws = get_sheet().worksheet("📋 PEDIDOS")
    rows = ws.get_all_values()[5:]
    nums = [r[0] for r in rows if r and r[0].startswith("PED-")]
    if not nums: return "PED-001"
    ultimo = max(int(n.replace("PED-","")) for n in nums if n.replace("PED-","").isdigit())
    return f"PED-{str(ultimo+1).zfill(3)}"

def get_siguiente_numero_factura():
    ws = get_sheet().worksheet("📋 PEDIDOS")
    rows = ws.get_all_values()[5:]
    nums = [r[8] for r in rows if len(r)>8 and r[8].startswith("FAC-")]
    if not nums: return "FAC-001"
    ultimo = max(int(n.replace("FAC-","")) for n in nums if n.replace("FAC-","").isdigit())
    return f"FAC-{str(ultimo+1).zfill(3)}"

def agregar_pedido(cliente, detalle, monto, fecha_entrega):
    sh = get_sheet()
    ws_ped = sh.worksheet("📋 PEDIDOS")
    nped = get_siguiente_numero_pedido()
    hoy = datetime.now().strftime("%d/%m/%Y")
    ws_ped.append_row([nped, cliente, detalle, hoy, fecha_entrega, monto, "RECIBIDO", "", "Cargado desde bot"])
    return nped

def registrar_factura(cliente, monto, vencimiento, nfac, detalle):
    sh = get_sheet()
    ws_ped = sh.worksheet("📋 PEDIDOS")
    ws_cob = sh.worksheet("💳 COBRANZAS")
    nped = get_siguiente_numero_pedido()
    hoy = datetime.now().strftime("%d/%m/%Y")
    ws_ped.append_row([nped, cliente, detalle, hoy, "", monto, "FACTURADO", nfac, "Cargado desde bot"])
    ws_cob.append_row([cliente, nped, nfac, monto, vencimiento, "PENDIENTE", "", "", "Cargado desde bot"])
    return nped, nfac

def registrar_pago(row_cobranza, fecha_cobro, metodo):
    ws = get_sheet().worksheet("💳 COBRANZAS")
    ws.update_cell(row_cobranza, 6, "COBRADO")
    ws.update_cell(row_cobranza, 7, fecha_cobro)
    ws.update_cell(row_cobranza, 8, metodo)

def get_resumen_cobrar():
    pendientes = get_cobranzas_pendientes()
    total = sum(p["monto"] for p in pendientes)
    vencidos = [p for p in pendientes if p["estado"] == "VENCIDO"]
    total_vencido = sum(p["monto"] for p in vencidos)
    return pendientes, total, vencidos, total_vencido

# ── CLAUDE VISION ─────────────────────────────────────────────────────────────
async def analizar_comprobante(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json",
                     "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": """Analizá este comprobante de pago recibido de un cliente B2B.
Extraé SOLO estos datos en JSON sin texto adicional:
{
  "monto": número sin símbolos,
  "pagador": "nombre de quien paga",
  "fecha": "DD/MM/YYYY",
  "referencia": "número de operación si aparece",
  "metodo": "Transferencia o Yape o Efectivo"
}
Si no podés leer algún dato, ponelo null."""}
                    ]
                }]
            }
        )
        text = resp.json()["content"][0]["text"]
        try:
            return json.loads(text.strip().replace("```json","").replace("```","").strip())
        except:
            return {"monto": None, "pagador": None, "fecha": None, "referencia": None, "metodo": None}

# ── HELPERS ───────────────────────────────────────────────────────────────────
def is_auth(uid): return not AUTHORIZED or AUTHORIZED == [""] or str(uid) in AUTHORIZED
def sol(n): return f"S/ {n:,.2f}"
def teclado_clientes():
    clientes = get_clientes()
    filas = [[InlineKeyboardButton(c, callback_data=f"cli_{c}")] for c in clientes]
    filas.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])
    return InlineKeyboardMarkup(filas)
def teclado_si_no(data_si, data_no="cancelar"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data=data_si),
        InlineKeyboardButton("❌ No / Cancelar", callback_data=data_no)
    ]])

# ── COMANDO START ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id):
        await update.message.reply_text("❌ Sin acceso.")
        return
    await update.message.reply_text(
        "💼 *PechuFree — Bot B2B*\n\n"
        "📸 Mandame la captura de un pago y lo registro\n\n"
        "*Comandos:*\n"
        "/factura — Cargar nueva factura emitida\n"
        "/pedido — Cargar pedido nuevo\n"
        "/cobrar — Ver qué falta cobrar\n"
        "/vencidos — Ver pagos vencidos\n"
        "/cliente — Estado de cuenta de un cliente\n"
        "/resumen — Resumen general del mes",
        parse_mode="Markdown"
    )

# ── /cobrar ───────────────────────────────────────────────────────────────────
async def cmd_cobrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    pendientes, total, vencidos, total_vencido = get_resumen_cobrar()
    if not pendientes:
        await update.message.reply_text("✅ Todo cobrado. No hay pendientes.")
        return
    texto = f"💳 *Por cobrar — {len(pendientes)} facturas*\n\n"
    if vencidos:
        texto += f"🔴 *VENCIDOS ({len(vencidos)}):*\n"
        for v in vencidos:
            texto += f"• {v['cliente']} — {sol(v['monto'])} ({v['pedido']})\n"
        texto += "\n"
    no_vencidos = [p for p in pendientes if p["estado"]=="PENDIENTE"]
    if no_vencidos:
        texto += f"🟡 *PENDIENTES ({len(no_vencidos)}):*\n"
        for p in no_vencidos:
            texto += f"• {p['cliente']} — {sol(p['monto'])} · vence {p['vencimiento']}\n"
    texto += f"\n💰 *Total: {sol(total)}*"
    if total_vencido > 0:
        texto += f"\n🔴 *Vencido: {sol(total_vencido)}*"
    await update.message.reply_text(texto, parse_mode="Markdown")

# ── /vencidos ─────────────────────────────────────────────────────────────────
async def cmd_vencidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    _, _, vencidos, total_vencido = get_resumen_cobrar()
    if not vencidos:
        await update.message.reply_text("✅ No hay pagos vencidos.")
        return
    texto = f"🔴 *Pagos vencidos — {len(vencidos)}*\n\n"
    for v in vencidos:
        texto += f"• *{v['cliente']}*\n  {sol(v['monto'])} · {v['pedido']} · venció {v['vencimiento']}\n\n"
    texto += f"💰 *Total vencido: {sol(total_vencido)}*"
    await update.message.reply_text(texto, parse_mode="Markdown")

# ── /resumen ──────────────────────────────────────────────────────────────────
async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    try:
        ws = get_sheet().worksheet("💳 COBRANZAS")
        rows = ws.get_all_values()[5:]
        total_cobrado = sum(float(r[3].replace(",","")) for r in rows if len(r)>5 and r[5]=="COBRADO" and r[3])
        total_pendiente = sum(float(r[3].replace(",","")) for r in rows if len(r)>5 and r[5] in ["PENDIENTE","VENCIDO"] and r[3])
        total_vencido = sum(float(r[3].replace(",","")) for r in rows if len(r)>5 and r[5]=="VENCIDO" and r[3])
        texto = (
            f"📊 *Resumen B2B — {datetime.now().strftime('%B %Y')}*\n\n"
            f"✅ Cobrado: {sol(total_cobrado)}\n"
            f"⏳ Pendiente: {sol(total_pendiente)}\n"
            f"🔴 Vencido: {sol(total_vencido)}\n"
            f"━━━━━━━━━━━━\n"
            f"💰 *Total facturado: {sol(total_cobrado + total_pendiente)}*"
        )
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── /cliente ──────────────────────────────────────────────────────────────────
async def cmd_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    await update.message.reply_text(
        "¿De qué cliente querés ver el estado?",
        reply_markup=teclado_clientes()
    )
    context.user_data['accion'] = 'ver_cliente'

# ── FLUJO: NUEVA FACTURA ──────────────────────────────────────────────────────
async def cmd_factura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    context.user_data.clear()
    context.user_data['factura'] = {}
    nfac = get_siguiente_numero_factura()
    context.user_data['factura']['nfac'] = nfac
    await update.message.reply_text(
        f"📄 *Nueva factura — {nfac}*\n\n¿A qué cliente?",
        parse_mode="Markdown",
        reply_markup=teclado_clientes()
    )
    return FACT_CLIENTE

async def fact_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END
    cliente = query.data.replace("cli_","")
    context.user_data['factura']['cliente'] = cliente
    await query.edit_message_text(f"✅ Cliente: *{cliente}*\n\n💰 ¿Cuál es el monto? (solo el número)", parse_mode="Markdown")
    return FACT_MONTO

async def fact_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = float(update.message.text.replace(",","").replace("S/","").strip())
        context.user_data['factura']['monto'] = monto
        await update.message.reply_text(f"✅ Monto: {sol(monto)}\n\n📅 ¿Cuándo vence? Escribí los días (ej: 30) o la fecha (DD/MM/YYYY)")
        return FACT_VENCIMIENTO
    except:
        await update.message.reply_text("❌ No entendí el monto. Escribí solo el número, ej: 850")
        return FACT_MONTO

async def fact_vencimiento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        if "/" in texto:
            vencimiento = texto
        else:
            dias = int(texto)
            vencimiento = (datetime.now() + timedelta(days=dias)).strftime("%d/%m/%Y")
        context.user_data['factura']['vencimiento'] = vencimiento
        await update.message.reply_text(f"✅ Vence: {vencimiento}\n\n📝 ¿Detalle del pedido? (ej: 12x Brownie caja + 6x Alfajor)")
        return FACT_DETALLE
    except:
        await update.message.reply_text("❌ No entendí. Escribí días (ej: 30) o fecha (ej: 15/04/2026)")
        return FACT_VENCIMIENTO

async def fact_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    detalle = update.message.text.strip()
    context.user_data['factura']['detalle'] = detalle
    f = context.user_data['factura']
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar y registrar", callback_data="confirmar_factura")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
    ])
    await update.message.reply_text(
        f"📋 *Confirmar factura:*\n\n"
        f"🏢 Cliente: {f['cliente']}\n"
        f"💰 Monto: {sol(f['monto'])}\n"
        f"📅 Vence: {f['vencimiento']}\n"
        f"🧾 N° Factura: {f['nfac']}\n"
        f"📦 Detalle: {detalle}",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return FACT_NUMERO

async def fact_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END
    f = context.user_data['factura']
    try:
        nped, nfac = registrar_factura(f['cliente'], f['monto'], f['vencimiento'], f['nfac'], f['detalle'])
        context.user_data.clear()
        await query.edit_message_text(
            f"✅ *Factura registrada*\n\n"
            f"🧾 {nfac} → {f['cliente']}\n"
            f"💰 {sol(f['monto'])}\n"
            f"📅 Vence: {f['vencimiento']}\n\n"
            f"El Sheet se actualizó automáticamente 🎉",
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Error al registrar: {e}")
    return ConversationHandler.END

# ── FLUJO: NUEVO PEDIDO ───────────────────────────────────────────────────────
async def cmd_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    context.user_data.clear()
    context.user_data['pedido'] = {}
    await update.message.reply_text("📦 *Nuevo pedido*\n\n¿De qué cliente?", parse_mode="Markdown", reply_markup=teclado_clientes())
    return PEDIDO_CLIENTE

async def pedido_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END
    context.user_data['pedido']['cliente'] = query.data.replace("cli_","")
    await query.edit_message_text(f"✅ Cliente: *{context.user_data['pedido']['cliente']}*\n\n📝 ¿Detalle del pedido?", parse_mode="Markdown")
    return PEDIDO_DETALLE

async def pedido_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['pedido']['detalle'] = update.message.text.strip()
    await update.message.reply_text("💰 ¿Monto del pedido?")
    return PEDIDO_MONTO

async def pedido_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['pedido']['monto'] = float(update.message.text.replace(",","").replace("S/",""))
        await update.message.reply_text("📅 ¿Fecha de entrega? (DD/MM/YYYY o días, ej: 3)")
        return PEDIDO_ENTREGA
    except:
        await update.message.reply_text("❌ Solo el número, ej: 480")
        return PEDIDO_MONTO

async def pedido_entrega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        fecha = texto if "/" in texto else (datetime.now() + timedelta(days=int(texto))).strftime("%d/%m/%Y")
        p = context.user_data['pedido']
        nped = agregar_pedido(p['cliente'], p['detalle'], p['monto'], fecha)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ *Pedido registrado*\n\n"
            f"📋 {nped} → {p['cliente']}\n"
            f"💰 {sol(p['monto'])}\n"
            f"📅 Entrega: {fecha}\n\n"
            f"Estado: RECIBIDO 🎉",
            parse_mode="Markdown"
        )
    except:
        await update.message.reply_text("❌ No entendí la fecha.")
        return PEDIDO_ENTREGA
    return ConversationHandler.END

# ── FOTOS: REGISTRAR PAGO RECIBIDO ────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id): return
    await update.message.reply_text("🔍 Analizando el comprobante de pago...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()
    datos = await analizar_comprobante(bytes(image_bytes))

    if not datos.get('monto'):
        await update.message.reply_text("❌ No pude leer el monto.\nUsá /cobrar para ver pendientes y marcar manualmente.")
        return

    monto = datos['monto']
    pagador = datos.get('pagador') or "No detectado"
    fecha = datos.get('fecha') or datetime.now().strftime("%d/%m/%Y")
    metodo = datos.get('metodo') or "Transferencia"

    # Buscar factura pendiente que coincida por monto
    pendientes = get_cobranzas_pendientes()
    matches = [p for p in pendientes if abs(p['monto'] - monto) < 5]

    context.user_data['pago'] = {'monto': monto, 'fecha': fecha, 'metodo': metodo, 'matches': matches}

    if matches:
        match = matches[0]
        context.user_data['pago']['match_row'] = match['row']
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí, registrar", callback_data="confirmar_pago_b2b")],
            [InlineKeyboardButton("🔍 Ver otros pendientes", callback_data="ver_pendientes_pago")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ])
        await update.message.reply_text(
            f"📋 *Pago detectado:*\n\n"
            f"💰 Monto: {sol(monto)}\n"
            f"🏢 Pagador: {pagador}\n"
            f"📅 Fecha: {fecha}\n"
            f"💳 Método: {metodo}\n\n"
            f"✅ Coincide con:\n"
            f"*{match['cliente']}* — {match['pedido']} ({sol(match['monto'])})\n\n"
            f"¿Registro el pago?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        # No hay match — mostrar lista de pendientes
        if pendientes:
            keyboard = [[InlineKeyboardButton(
                f"{p['cliente']} — {sol(p['monto'])}",
                callback_data=f"pago_row_{p['row']}"
            )] for p in pendientes[:6]]
            keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])
            await update.message.reply_text(
                f"💰 Detecté un pago de *{sol(monto)}*\n\n"
                f"No encontré una factura por ese monto exacto.\n"
                f"¿A cuál de estos corresponde?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(f"ℹ️ Detecté un pago de {sol(monto)} pero no hay facturas pendientes.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelado.")

    elif data == "confirmar_pago_b2b":
        pago = context.user_data.get('pago', {})
        row = pago.get('match_row')
        if row:
            registrar_pago(row, pago['fecha'], pago['metodo'])
            context.user_data.clear()
            await query.edit_message_text(
                f"✅ *Pago registrado*\n\n"
                f"💰 {sol(pago['monto'])} — {pago['metodo']}\n"
                f"📅 {pago['fecha']}\n\n"
                f"El Sheet se actualizó automáticamente 🎉",
                parse_mode="Markdown"
            )

    elif data.startswith("pago_row_"):
        row = int(data.replace("pago_row_",""))
        pago = context.user_data.get('pago', {})
        pago['match_row'] = row
        registrar_pago(row, pago.get('fecha', datetime.now().strftime("%d/%m/%Y")), pago.get('metodo','Transferencia'))
        context.user_data.clear()
        await query.edit_message_text(
            f"✅ *Pago registrado correctamente*\n\n"
            f"💰 {sol(pago.get('monto',0))}\n"
            f"📅 {pago.get('fecha','')}\n\n"
            f"El Sheet se actualizó 🎉",
            parse_mode="Markdown"
        )

    elif data.startswith("cli_") and context.user_data.get('accion') == 'ver_cliente':
        cliente = data.replace("cli_","")
        pendientes = [p for p in get_cobranzas_pendientes() if p['cliente'] == cliente]
        total_pendiente = sum(p['monto'] for p in pendientes)
        texto = f"📒 *Estado de cuenta — {cliente}*\n\n"
        if pendientes:
            texto += f"⏳ Facturas pendientes ({len(pendientes)}):\n"
            for p in pendientes:
                icon = "🔴" if p['estado']=="VENCIDO" else "🟡"
                texto += f"{icon} {p['pedido']} — {sol(p['monto'])} · vence {p['vencimiento']}\n"
            texto += f"\n💰 *Total pendiente: {sol(total_pendiente)}*"
        else:
            texto += "✅ Sin deudas pendientes"
        context.user_data.clear()
        await query.edit_message_text(texto, parse_mode="Markdown")

# ── CANCELAR CONVERSACIÓN ────────────────────────────────────────────────────
async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversación: nueva factura
    conv_factura = ConversationHandler(
        entry_points=[CommandHandler("factura", cmd_factura)],
        states={
            FACT_CLIENTE: [CallbackQueryHandler(fact_cliente)],
            FACT_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_monto)],
            FACT_VENCIMIENTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_vencimiento)],
            FACT_DETALLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_detalle)],
            FACT_NUMERO: [CallbackQueryHandler(fact_confirmar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )

    # Conversación: nuevo pedido
    conv_pedido = ConversationHandler(
        entry_points=[CommandHandler("pedido", cmd_pedido)],
        states={
            PEDIDO_CLIENTE: [CallbackQueryHandler(pedido_cliente)],
            PEDIDO_DETALLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_detalle)],
            PEDIDO_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_monto)],
            PEDIDO_ENTREGA: [MessageHandler(filters.TEXT & ~filters.COMMAND, pedido_entrega)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cobrar", cmd_cobrar))
    app.add_handler(CommandHandler("vencidos", cmd_vencidos))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("cliente", cmd_cliente))
    # ConversationHandlers ANTES del CallbackQueryHandler genérico
    app.add_handler(conv_factura)
    app.add_handler(conv_pedido)
    # Fotos y callbacks generales AL FINAL
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Bot B2B PechuFree iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
