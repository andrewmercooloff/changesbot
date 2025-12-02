import os
import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Dict, Optional
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Глобальные переменные для хранения состояния
# Ключ - chat_id, значение - словарь с url и последним хешем
user_data: Dict[int, Dict[str, str]] = {}
# Флаг для отслеживания активных задач проверки
monitoring_tasks: Dict[int, asyncio.Task] = {}


async def fetch_page_content(url: str) -> Optional[str]:
    """Получает содержимое страницы по URL"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 200:
                    content = await response.text()
                    return content
                else:
                    logger.error(f"Ошибка при получении страницы: статус {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Ошибка при запросе страницы {url}: {e}")
        return None


def calculate_hash(content: str) -> str:
    """Вычисляет хеш содержимого страницы"""
    return hashlib.md5(content.encode('utf-8')).hexdigest()


async def check_page_changes(chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет изменения на странице и отправляет уведомление при изменении"""
    content = await fetch_page_content(url)
    
    if content is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Проблема при проверке страницы\n\n"
                f"🔗 Страница: {url}\n\n"
                f"❌ Не удалось получить содержимое страницы.\n"
                f"Возможные причины:\n"
                f"• Страница временно недоступна\n"
                f"• Проблемы с интернет-соединением\n"
                f"• Страница требует авторизации\n\n"
                f"🔄 Я попробую снова при следующей проверке (через 1 час)"
            )
        )
        return
    
    current_hash = calculate_hash(content)
    user_info = user_data.get(chat_id, {})
    last_hash = user_info.get('last_hash')
    
    if last_hash is None:
        # Первая проверка - сохраняем хеш
        user_data[chat_id] = {
            'url': url,
            'last_hash': current_hash,
            'last_check': datetime.now().isoformat()
        }
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Отслеживание успешно начато!\n\n"
                f"🔗 Отслеживаемая страница:\n{url}\n\n"
                f"✅ Первая проверка выполнена успешно\n"
                f"📊 Страница сохранена как эталон\n\n"
                f"⏰ Следующая проверка будет через 1 час\n"
                f"🔔 Я отправлю уведомление, если обнаружу изменения на странице"
            )
        )
    elif current_hash != last_hash:
        # Страница изменилась!
        user_data[chat_id]['last_hash'] = current_hash
        user_data[chat_id]['last_check'] = datetime.now().isoformat()
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔔 ВНИМАНИЕ! Обнаружены изменения!\n\n"
                f"🔗 Страница: {url}\n\n"
                f"⏰ Время обнаружения: {datetime.now().strftime('%d.%m.%Y в %H:%M:%S')}\n\n"
                f"📝 Страница была изменена. Проверьте её содержимое!"
            )
        )
        logger.info(f"Изменения обнаружены для пользователя {chat_id} на странице {url}")
    else:
        # Изменений нет
        user_data[chat_id]['last_check'] = datetime.now().isoformat()
        logger.debug(f"Изменений не обнаружено для пользователя {chat_id}")


async def monitoring_loop(chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE):
    """Основной цикл мониторинга страницы"""
    while chat_id in user_data:
        try:
            await check_page_changes(chat_id, url, context)
            # Ждем 1 час (3600 секунд) перед следующей проверкой
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info(f"Мониторинг остановлен для пользователя {chat_id}")
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга для пользователя {chat_id}: {e}")
            await asyncio.sleep(60)  # Ждем минуту перед повторной попыткой


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    
    welcome_message = (
        "👋 Привет! Я бот для отслеживания изменений на веб-страницах.\n\n"
        "📋 Как это работает:\n"
        "1️⃣ Отправьте мне ссылку на страницу, которую хотите отслеживать\n"
        "2️⃣ Я сохраню ссылку и начну проверять её каждый час\n"
        "3️⃣ Когда на странице что-то изменится, я сразу отправлю вам уведомление\n\n"
        "📝 Что нужно сделать сейчас:\n"
        "Просто отправьте мне ссылку на страницу (например: https://example.com)\n\n"
        "📌 Доступные команды:\n"
        "/start - показать это сообщение\n"
        "/stop - остановить отслеживание текущей страницы\n"
        "/status - узнать статус отслеживания"
    )
    
    await update.message.reply_text(welcome_message)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сообщений с URL"""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # Простая проверка на URL
    if not (text.startswith('http://') or text.startswith('https://')):
        await update.message.reply_text(
            "❌ Ошибка: некорректная ссылка!\n\n"
            "Пожалуйста, отправьте ссылку, которая начинается с:\n"
            "• http://\n"
            "• https://\n\n"
            "Пример правильной ссылки:\n"
            "https://example.com"
        )
        return
    
    # Останавливаем предыдущий мониторинг, если он был
    if chat_id in monitoring_tasks:
        monitoring_tasks[chat_id].cancel()
        del monitoring_tasks[chat_id]
        await update.message.reply_text(
            "⚠️ Предыдущее отслеживание остановлено. Начинаю отслеживание новой ссылки."
        )
    
    # Очищаем предыдущие данные
    user_data[chat_id] = {
        'url': text,
        'last_hash': None,
        'last_check': None
    }
    
    await update.message.reply_text(
        f"✅ Отлично! Я принял вашу ссылку:\n{text}\n\n"
        "🔄 Что происходит сейчас:\n"
        "• Сохраняю ссылку для отслеживания\n"
        "• Выполняю первую проверку страницы\n"
        "• Настраиваю автоматические проверки каждый час\n\n"
        "⏳ Пожалуйста, подождите несколько секунд..."
    )
    
    # Запускаем мониторинг
    task = asyncio.create_task(monitoring_loop(chat_id, text, context))
    monitoring_tasks[chat_id] = task
    
    # Выполняем первую проверку сразу
    await check_page_changes(chat_id, text, context)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop"""
    chat_id = update.effective_chat.id
    
    if chat_id in monitoring_tasks:
        monitoring_tasks[chat_id].cancel()
        del monitoring_tasks[chat_id]
    
    if chat_id in user_data:
        del user_data[chat_id]
    
    await update.message.reply_text(
        "⏹ Отслеживание остановлено\n\n"
        "✅ Все данные об отслеживании удалены\n"
        "✅ Автоматические проверки прекращены\n\n"
        "📝 Чтобы начать отслеживание новой страницы:\n"
        "Отправьте мне новую ссылку на страницу, которую хотите отслеживать."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    chat_id = update.effective_chat.id
    
    if chat_id not in user_data:
        await update.message.reply_text(
            "❌ Отслеживание не активно\n\n"
            "📝 Чтобы начать отслеживание:\n"
            "Отправьте мне ссылку на страницу, которую хотите отслеживать.\n\n"
            "Пример: https://example.com"
        )
        return
    
    user_info = user_data[chat_id]
    url = user_info.get('url', 'Не указано')
    last_check = user_info.get('last_check', 'Ещё не выполнена')
    
    # Форматируем время последней проверки для лучшей читаемости
    if last_check and last_check != 'Ещё не выполнена':
        try:
            check_time = datetime.fromisoformat(last_check)
            formatted_time = check_time.strftime('%d.%m.%Y в %H:%M:%S')
        except:
            formatted_time = last_check
    else:
        formatted_time = last_check
    
    status_message = (
        f"📊 Текущий статус отслеживания:\n\n"
        f"🔗 Отслеживаемая страница:\n{url}\n\n"
        f"⏰ Последняя проверка:\n{formatted_time}\n\n"
        f"🔄 Режим работы:\n"
        f"Автоматическая проверка каждый час\n\n"
        f"✅ Отслеживание активно"
    )
    
    await update.message.reply_text(status_message)


def main():
    """Основная функция запуска бота"""
    # Получаем токен бота из переменных окружения
    bot_token = os.getenv('BOT_TOKEN')
    
    if not bot_token:
        logger.error("BOT_TOKEN не найден в переменных окружения!")
        print("Ошибка: BOT_TOKEN не найден!")
        print("Создайте файл .env и добавьте туда BOT_TOKEN=ваш_токен")
        return
    
    # Создаем приложение
    application = Application.builder().token(bot_token).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    # Запускаем бота
    logger.info("Бот запущен...")
    print("Бот запущен! Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

