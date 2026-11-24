import os
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Carrega .env localmente (no Render, usa automaticamente as Environment Variables do painel)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
SITE_API_URL = os.getenv("SITE_API_URL", "").strip()
SITE_API_KEY = os.getenv("SITE_API_KEY", "").strip()
ALLOWED_USER_ID = int((os.getenv("ALLOWED_USER_ID") or "0").strip())

if not TELEGRAM_TOKEN or not SITE_API_URL or not SITE_API_KEY:
    raise ValueError(
        "Preencha TELEGRAM_TOKEN, SITE_API_URL e SITE_API_KEY nas variáveis de ambiente."
    )

# Fuso horário de Brasília (UTC-3)
TZ_BRASIL = timezone(timedelta(hours=-3))

# Estados da conversa
TITULO, DATA, HORA, CONFIRMACAO = range(4)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Mini-servidor HTTP exigido pelo Render (Web Service Free) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot online")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Evita poluir os logs do Render com pings do UptimeRobot


def iniciar_servidor_health() -> None:
    port = int(os.environ.get("PORT", "10000"))
    logger.info("Servidor HTTP aberto na porta %s", port)
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# --- Lógica do Bot ---
def usuario_autorizado(update: Update) -> bool:
    """Garante que apenas o dono configurado consiga criar agendamentos."""
    user = update.effective_user
    return user is not None and ALLOWED_USER_ID != 0 and user.id == ALLOWED_USER_ID


async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bot de agenda online.\n\n"
        "Comandos disponíveis:\n"
        "/agendar - Marcar novo evento no site\n"
        "/cancelar - Cancelar operação atual\n"
        "/meuid - Ver seu ID do Telegram"
    )


async def comando_meuid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.message.reply_text(
            f"Seu User ID numérico é: `{user.id}`\n"
            "Coloque esse número na variável ALLOWED_USER_ID.",
            parse_mode="Markdown",
        )


async def iniciar_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not usuario_autorizado(update):
        await update.message.reply_text(
            "Acesso negado. Use /meuid para verificar se seu ID está liberado em ALLOWED_USER_ID."
        )
        return ConversationHandler.END

    context.user_data.clear()

    await update.message.reply_text(
        "Vamos criar um novo agendamento.\n"
        "1/3 - Qual é o título do compromisso?\n\n"
        "(Envie /cancelar a qualquer momento para desistir.)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TITULO


async def receber_titulo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    titulo = update.message.text.strip()

    if len(titulo) < 2 or len(titulo) > 120:
        await update.message.reply_text(
            "O título precisa ter entre 2 e 120 caracteres. Digite novamente:"
        )
        return TITULO

    context.user_data["titulo"] = titulo
    await update.message.reply_text(
        "2/3 - Qual é a data?\n"
        "Use exatamente o formato DD/MM/AAAA (exemplo: 28/11/2026):"
    )
    return DATA


async def receber_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto_data = update.message.text.strip()

    try:
        data_valida = datetime.strptime(texto_data, "%d/%m/%Y").date()
    except ValueError:
        await update.message.reply_text(
            "Data inválida. Digite no formato DD/MM/AAAA (exemplo: 28/11/2026):"
        )
        return DATA

    context.user_data["data"] = data_valida
    await update.message.reply_text(
        "3/3 - Qual é o horário?\n"
        "Use o formato 24h HH:MM (exemplo: 14:30):"
    )
    return HORA


async def receber_hora(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto_hora = update.message.text.strip()

    try:
        hora_valida = datetime.strptime(texto_hora, "%H:%M").time()
    except ValueError:
        await update.message.reply_text(
            "Horário inválido. Digite no formato 24h HH:MM (exemplo: 09:00 ou 18:30):"
        )
        return HORA

    dt_completo = datetime.combine(
        context.user_data["data"], hora_valida, tzinfo=TZ_BRASIL
    )
    context.user_data["data_hora_iso"] = dt_completo.isoformat()
    context.user_data["data_hora_legivel"] = dt_completo.strftime("%d/%m/%Y às %H:%M")

    teclado = ReplyKeyboardMarkup(
        [["Sim", "Não"]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "Confira os dados antes de enviar para o site:\n\n"
        f"Compromisso: {context.user_data['titulo']}\n"
        f"Quando: {context.user_data['data_hora_legivel']}\n\n"
        "Deseja confirmar e salvar na agenda?",
        reply_markup=teclado,
    )
    return CONFIRMACAO


async def confirmar_envio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    resposta = update.message.text.strip().lower()

    if resposta in ("não", "nao"):
        context.user_data.clear()
        await update.message.reply_text(
            "Agendamento cancelado. Nada foi enviado ao site.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if resposta != "sim":
        await update.message.reply_text("Responda usando os botões 'Sim' ou 'Não'.")
        return CONFIRMACAO

    payload = {
        "titulo": context.user_data["titulo"],
        "data_hora_iso": context.user_data["data_hora_iso"],
        "origem": "telegram_bot",
    }

    headers = {
        "Authorization": f"Bearer {SITE_API_KEY}",
        "Content-Type": "application/json",
    }

    await update.message.reply_text(
        "Enviando para o site...",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(SITE_API_URL, json=payload, headers=headers)

        if response.status_code in (200, 201):
            await update.message.reply_text(
                "Agendado com sucesso no site!\n\n"
                f"Compromisso: {payload['titulo']}\n"
                f"Data/Hora: {context.user_data['data_hora_legivel']}"
            )
        elif response.status_code in (401, 403):
            logger.error("Erro de autenticação na API do site: %s", response.text)
            await update.message.reply_text(
                "Erro de autenticação: a SITE_API_KEY do bot não bate com a do site."
            )
        else:
            logger.error("API retornou %s: %s", response.status_code, response.text)
            await update.message.reply_text(
                f"O site recusou o agendamento (HTTP {response.status_code}). "
                "Verifique o log do servidor."
            )

    except httpx.TimeoutException:
        await update.message.reply_text(
            "O site demorou demais para responder (timeout). Tente novamente em instantes."
        )
    except httpx.RequestError as exc:
        logger.error("Falha de conexão com o site: %s", exc)
        await update.message.reply_text(
            "Não consegui conectar ao site. Verifique se o servidor está online e se a SITE_API_URL está correta."
        )
    finally:
        context.user_data.clear()

    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Operação cancelada.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def main() -> None:
    # Inicia a porta HTTP antes do polling para o Render marcar o serviço como "Live"
    threading.Thread(target=iniciar_servidor_health, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("agendar", iniciar_agendamento)],
        states={
            TITULO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_titulo)],
            DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_data)],
            HORA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_hora)],
            CONFIRMACAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_envio)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", comando_start))
    app.add_handler(CommandHandler("meuid", comando_meuid))
    app.add_handler(conv_handler)

    logger.info("Bot iniciado com sucesso. Aguardando comandos...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
