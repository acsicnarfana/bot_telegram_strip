import os
import logging
import sqlite3
import stripe
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)
from flask import Flask, request, jsonify

# Configurações CORRIGIDAS - usar os.getenv() corretamente
TOKEN = os.getenv("TELEGRAM_TOKEN")
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6822352679"))  # Convertendo string para int
DOMAIN = os.getenv("DOMAIN")  # Para webhooks

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_errors.log')
    ]
)
logger = logging.getLogger(__name__)

# Inicializar Stripe
stripe.api_key = STRIPE_API_KEY

# Estados da conversa
PRODUCT, PRICE, DESCRIPTION, GROUP_LINK, RECURRING = range(5)

# Inicializar banco de dados
def init_db():
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS products (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT NOT NULL,
                 price_id TEXT NOT NULL,
                 description TEXT,
                 group_link TEXT NOT NULL,
                 is_recurring BOOLEAN NOT NULL DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS customers (
                 user_id INTEGER PRIMARY KEY,
                 stripe_id TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_products (
                 user_id INTEGER,
                 product_id INTEGER,
                 purchase_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 PRIMARY KEY (user_id, product_id))''')
    
    conn.commit()
    conn.close()

init_db()

# Variável global para o bot (usada no webhook)
bot_instance = None

# ======================
# FUNÇÕES ADMINISTRATIVAS
# ======================
async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Acesso restrito!")
        return ConversationHandler.END
        
    await update.message.reply_text("📝 Vamos criar um novo produto!\nEnvie o nome do grupo VIP:")
    return PRODUCT

async def product_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['product_name'] = update.message.text
    await update.message.reply_text("💵 Agora envie o preço mensal em USD (ex: 10.99):")
    return PRICE

async def product_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text)
        context.user_data['price'] = price
        await update.message.reply_text("📄 Envie a descrição do grupo:")
        return DESCRIPTION
    except ValueError:
        await update.message.reply_text("⚠️ Preço inválido! Envie um número (ex: 10.99)")
        return PRICE

async def product_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['description'] = update.message.text
    await update.message.reply_text("🔗 Agora envie o link de convite do grupo (deve começar com https://t.me/):")
    return GROUP_LINK

async def group_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text
    if not link.startswith("https://t.me/"):
        await update.message.reply_text("❌ Formato inválido! O link deve começar com https://t.me/")
        return GROUP_LINK
        
    context.user_data['group_link'] = link
    
    keyboard = [
        [InlineKeyboardButton("Sim", callback_data='recurring_yes')],
        [InlineKeyboardButton("Não", callback_data='recurring_no')]
    ]
    await update.message.reply_text(
        "🔄 É uma assinatura recorrente?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return RECURRING

async def recurring_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    is_recurring = query.data == 'recurring_yes'
    context.user_data['is_recurring'] = is_recurring
    
    # Criar produto no Stripe
    try:
        product = stripe.Product.create(name=context.user_data['product_name'])
        
        if is_recurring:
            # Assinatura recorrente
            price = stripe.Price.create(
                unit_amount=int(context.user_data['price'] * 100),
                currency='usd',
                recurring={"interval": "month"},
                product=product.id
            )
        else:
            # Pagamento único
            price = stripe.Price.create(
                unit_amount=int(context.user_data['price'] * 100),
                currency='usd',
                product=product.id
            )
        
        # Salvar no banco de dados
        conn = sqlite3.connect('vip_groups.db', timeout=10)
        c = conn.cursor()
        c.execute(
            "INSERT INTO products (name, price_id, description, group_link, is_recurring) VALUES (?, ?, ?, ?, ?)",
            (
                context.user_data['product_name'],
                price.id,
                context.user_data['description'],
                context.user_data['group_link'],
                int(is_recurring)
            )
        )
        conn.commit()
        conn.close()
        
        await query.edit_message_text("✅ Produto criado com sucesso!")
    except stripe.error.StripeError as e:
        logger.error(f"Erro Stripe: {e.user_message}")
        await query.edit_message_text(f"❌ Erro Stripe: {e.user_message}")
    except Exception as e:
        logger.error(f"Erro ao criar produto: {e}")
        await query.edit_message_text("❌ Erro interno ao criar produto")
    
    return ConversationHandler.END

# ======================
# FUNÇÕES PARA USUÁRIOS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await update.message.reply_text(
        f"👋 Olá {user.first_name}!\n"
        "Bem-vindo ao gerenciador de grupos VIP!\n\n"
        "Use /comprar para ver os grupos disponíveis\n"
        "Use /meusacessos para ver seus produtos comprados"
    )

async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    c.execute("SELECT id, name, description, is_recurring FROM products")
    products = c.fetchall()
    conn.close()

    if not products:
        await update.message.reply_text("ℹ️ Nenhum grupo VIP disponível no momento.")
        return

    keyboard = []
    for prod in products:
        product_type = "🔁 Assinatura" if prod[3] else "✅ Acesso Vitalício"
        keyboard.append([
            InlineKeyboardButton(
                f"{prod[1]} - {product_type}", 
                callback_data=f"buy_{prod[0]}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🎁 Escolha um grupo VIP:", 
        reply_markup=reply_markup
    )

async def initiate_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = query.data.split("_")[1]
    user_id = query.from_user.id
    
    # Obter informações do produto
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    c.execute("SELECT name, price_id, group_link, is_recurring FROM products WHERE id=?", (product_id,))
    product = c.fetchone()
    conn.close()
    
    if not product:
        await query.edit_message_text("❌ Produto não encontrado")
        return
    
    product_name, price_id, group_link, is_recurring = product
    
    try:
        # Verificar se usuário já existe no Stripe
        conn = sqlite3.connect('vip_groups.db', timeout=10)
        c = conn.cursor()
        c.execute("SELECT stripe_id FROM customers WHERE user_id=?", (user_id,))
        customer = c.fetchone()
        
        if customer:
            customer_id = customer[0]
        else:
            # Criar novo cliente no Stripe
            tg_user = query.from_user
            customer = stripe.Customer.create(
                email=f"{tg_user.id}@telegram.user",
                name=f"{tg_user.first_name} {tg_user.last_name or ''}".strip(),
                metadata={"telegram_id": user_id}
            )
            customer_id = customer.id
            
            # Salvar no banco de dados
            c.execute(
                "INSERT INTO customers (user_id, stripe_id) VALUES (?, ?)",
                (user_id, customer_id)
            )
            conn.commit()
        
        conn.close()
        
        # Criar sessão de pagamento
        if is_recurring:
            # Assinatura recorrente
            session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=f'{DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{DOMAIN}/cancel',
                metadata={
                    "telegram_id": user_id,
                    "product_id": product_id
                }
            )
        else:
            # Pagamento único
            session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='payment',
                success_url=f'{DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{DOMAIN}/cancel',
                metadata={
                    "telegram_id": user_id,
                    "product_id": product_id
                }
            )
        
        # Enviar link de pagamento
        keyboard = [[InlineKeyboardButton("💳 Pagar Agora", url=session.url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✅ Clique abaixo para pagar pelo acesso a *{product_name}*\n"
            "Você será redirecionado para uma página segura do Stripe",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
    except stripe.error.StripeError as e:
        logger.error(f"Erro Stripe: {e.user_message}")
        await query.edit_message_text(f"❌ Erro no pagamento: {e.user_message}")
    except Exception as e:
        logger.error(f"Erro no pagamento: {str(e)}")
        await query.edit_message_text("❌ Ocorreu um erro interno ao processar seu pagamento")

async def my_purchases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    c.execute("""
        SELECT p.name, p.group_link 
        FROM products p
        JOIN user_products up ON p.id = up.product_id
        WHERE up.user_id = ?
    """, (user_id,))
    
    products = c.fetchall()
    conn.close()
    
    if not products:
        await update.message.reply_text("ℹ️ Você ainda não comprou nenhum grupo VIP.")
        return
    
    response = "🔑 Seus acessos VIP:\n\n"
    for idx, (name, link) in enumerate(products, 1):
        response += f"{idx}. [{name}]({link})\n"
    
    await update.message.reply_text(response, parse_mode='Markdown', disable_web_page_preview=True)

# ======================
# WEBHOOK PARA PAGAMENTOS
# ======================
app = Flask(__name__)

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except ValueError as e:
        logger.error(f"Payload inválido: {str(e)}")
        return 'Payload inválido', 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Assinatura inválida: {str(e)}")
        return 'Assinatura inválida', 400

    # Processar eventos importantes
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        handle_payment_success(session)
        
    elif event['type'] == 'invoice.paid':
        invoice = event['data']['object']
        handle_recurring_payment(invoice)
        
    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        handle_subscription_canceled(subscription)
        
    return jsonify(success=True), 200

def handle_payment_success(session):
    telegram_id = session.metadata.get('telegram_id')
    product_id = session.metadata.get('product_id')
    
    if not telegram_id or not product_id:
        logger.error("Metadados ausentes na sessão")
        return
    
    telegram_id = int(telegram_id)
    product_id = int(product_id)
    
    # Obter link do grupo
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    c.execute("SELECT group_link FROM products WHERE id=?", (product_id,))
    result = c.fetchone()
    if not result:
        logger.error(f"Produto {product_id} não encontrado")
        conn.close()
        return
    group_link = result[0]
    
    # Registrar compra na tabela user_products
    try:
        c.execute(
            "INSERT INTO user_products (user_id, product_id) VALUES (?, ?)",
            (telegram_id, product_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        logger.warning(f"Produto {product_id} já comprado pelo usuário {telegram_id}")
    conn.close()
    
    # Enviar acesso ao usuário
    try:
        global bot_instance
        if bot_instance:
            bot_instance.send_message(
                chat_id=telegram_id,
                text=f"🎉 Pagamento confirmado! Aqui está o acesso ao grupo VIP:\n{group_link}"
            )
        else:
            logger.error("Bot instance não disponível para enviar mensagem")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem: {str(e)}")

def handle_recurring_payment(invoice):
    # Lógica para renovação de assinatura
    customer_id = invoice.customer
    telegram_id = get_telegram_id(customer_id)
    
    if telegram_id:
        # Enviar confirmação de renovação
        try:
            bot_instance.send_message(
                chat_id=telegram_id,
                text="🔄 Sua assinatura foi renovada com sucesso! Seu acesso continua ativo."
            )
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem de renovação: {str(e)}")

def handle_subscription_canceled(subscription):
    # Lógica para assinatura cancelada
    customer_id = subscription.customer
    telegram_id = get_telegram_id(customer_id)
    
    if telegram_id:
        # Enviar notificação
        try:
            bot_instance.send_message(
                chat_id=telegram_id,
                text="⚠️ Sua assinatura foi cancelada. Seu acesso será encerrado no final do período pago."
            )
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem de cancelamento: {str(e)}")

def get_telegram_id(customer_id):
    conn = sqlite3.connect('vip_groups.db', timeout=10)
    c = conn.cursor()
    c.execute("SELECT user_id FROM customers WHERE stripe_id=?", (customer_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

# ======================
# CONFIGURAÇÃO DO BOT PARA RENDER
# ======================
def setup_bot():
    global bot_instance
    application = Application.builder().token(TOKEN).build()
    bot_instance = application.bot

    # Handlers de comando
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("comprar", show_products))
    application.add_handler(CommandHandler("meusacessos", my_purchases))

    # Handler para adicionar produtos (conversação)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('add', add_product)],
        states={
            PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_name)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_price)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_description)],
            GROUP_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_link)],
            RECURRING: [CallbackQueryHandler(recurring_choice, pattern='^recurring_')]
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: ConversationHandler.END)]
    )
    application.add_handler(conv_handler)

    # Handler para botões de compra
    application.add_handler(CallbackQueryHandler(initiate_payment, pattern='^buy_'))
    
    return application

# Configurar rota para webhook do Telegram
@app.route('/telegram-webhook', methods=['POST'])
async def telegram_webhook():
    application = setup_bot()
    json_data = await request.get_json()
    update = Update.de_json(json_data, application.bot)
    await application.process_update(update)
    return jsonify(success=True)

# Rota de verificação de saúde
@app.route('/')
def health_check():
    return "Bot VIP Telegram está online!"

# Ponto de entrada principal
if __name__ == '__main__':
    # Configurar webhook do Telegram
    application = setup_bot()
    application.bot.set_webhook(
        url=f"{DOMAIN}/telegram-webhook",
        allowed_updates=Update.ALL_TYPES
    )
    
    # Iniciar servidor Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
