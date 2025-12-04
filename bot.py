import os
import asyncio
import hashlib
import logging
import uuid
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor
import aiohttp
import cloudscraper
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from dotenv import load_dotenv

# Пробуем импортировать curl_cffi, если доступен
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# Загружаем переменные окружения
load_dotenv()

# Часовой пояс (UTC+3 для Москвы/Минска)
MOSCOW_TZ = timezone(timedelta(hours=3))

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Логируем доступность curl_cffi
if not CURL_CFFI_AVAILABLE:
    logger.warning("curl_cffi не установлен, будет использован только cloudscraper. Для лучшего обхода Cloudflare установите: pip install curl-cffi")


@dataclass
class Project:
    """Класс для хранения информации о проекте отслеживания"""
    project_id: str
    url: str
    name: str
    last_hash: Optional[str] = None
    last_check: Optional[str] = None
    interval_minutes: int = 60  # Периодичность проверки в минутах
    is_active: bool = True
    notify_on_no_changes: bool = False  # Отправлять уведомления при отсутствии изменений
    last_notification: Optional[str] = None  # Время последнего уведомления
    rss_url: Optional[str] = None  # URL RSS-ленты, если найдена
    last_rss_items: Optional[List[str]] = None  # Список ID последних новостей из RSS

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


# Глобальные переменные для хранения состояния
# Ключ - chat_id, значение - список проектов
user_projects: Dict[int, Dict[str, Project]] = {}
# Ключ - (chat_id, project_id), значение - задача мониторинга
monitoring_tasks: Dict[tuple, asyncio.Task] = {}

# Executor для синхронных операций (cloudscraper)
executor = ThreadPoolExecutor(max_workers=5)


def _fetch_with_curl_cffi(url: str) -> Optional[str]:
    """Попытка обхода Cloudflare через curl_cffi (более эффективный метод)"""
    if not CURL_CFFI_AVAILABLE:
        return None
    
    try:
        logger.info(f"Попытка обхода Cloudflare через curl_cffi для {url}")
        
        # Эмулируем реального пользователя из Беларуси
        # Случайная задержка перед запросом (имитация чтения страницы)
        time.sleep(random.uniform(2, 5))
        
        # Используем реалистичный браузер Chrome на Windows (популярный в Беларуси)
        response = curl_requests.get(
            url,
            impersonate="chrome120",  # Эмулируем Chrome 120
            timeout=60,
            allow_redirects=True,
            headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'ru-BY,ru;q=0.9,be;q=0.8,en-US;q=0.7,en;q=0.6',  # Беларусь
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        )
        
        logger.info(f"curl_cffi получил ответ со статусом {response.status_code} для {url}")
        
        if response.status_code == 200:
            content = response.text
            if content and len(content) > 100:
                content_lower = content.lower()
                if not any(indicator in content_lower for indicator in [
                    'checking your browser', 'ddos protection',
                    'please wait', 'just a moment', 'captcha', 'recaptcha',
                    'cloudflare', 'cf-browser-verification'
                ]):
                    logger.info(f"✅ Успешно получен контент через curl_cffi для {url}")
                    return content
        
        return None
    except Exception as e:
        logger.warning(f"Ошибка curl_cffi для {url}: {e}")
        return None


def _fetch_with_cloudscraper(url: str) -> Optional[str]:
    """Синхронная функция для получения страницы через cloudscraper (обход Cloudflare)"""
    try:
        logger.info(f"Попытка обхода Cloudflare через cloudscraper для {url}")
        
        # Эмулируем реального пользователя из Беларуси
        # Случайная задержка (имитация чтения страницы перед переходом)
        time.sleep(random.uniform(1, 3))
        
        # Настройки для пользователя из Беларуси (Windows + Chrome - самый популярный)
        browser_config = {
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
        
        scraper = cloudscraper.create_scraper(
            browser=browser_config,
            delay=random.randint(10, 20),  # Случайная задержка для Cloudflare challenge
            debug=False
        )
        
        # Реалистичные заголовки для пользователя из Беларуси
        scraper.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'ru-BY,ru;q=0.9,be;q=0.8,en-US;q=0.7,en;q=0.6',  # Беларусь
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        })
        
        # Добавляем Referer (если можем определить домен)
        try:
            parsed = urlparse(url)
            domain = f"{parsed.scheme}://{parsed.netloc}"
            scraper.headers['Referer'] = domain
        except:
            pass
        
        # Имитация движения мыши - делаем несколько запросов перед основным
        # (как будто пользователь кликает по ссылкам)
        try:
            # Сначала запрашиваем главную страницу (если это не главная)
            if '/news' in url or len(url.split('/')) > 4:
                main_url = f"{parsed.scheme}://{parsed.netloc}/"
                time.sleep(random.uniform(0.5, 1.5))
                scraper.get(main_url, timeout=30, allow_redirects=True)
                time.sleep(random.uniform(1, 2))  # Имитация чтения
        except:
            pass  # Игнорируем ошибки предварительных запросов
        
        # Основной запрос
        response = scraper.get(url, timeout=60, allow_redirects=True)
        logger.info(f"Cloudscraper получил ответ со статусом {response.status_code} для {url}")
        
        if response.status_code == 200:
            content = response.text
            if content and len(content) > 100:
                content_lower = content.lower()
                if not any(indicator in content_lower for indicator in [
                    'checking your browser', 'ddos protection',
                    'please wait', 'just a moment', 'captcha', 'recaptcha',
                    'cloudflare', 'cf-browser-verification'
                ]):
                    logger.info(f"✅ Успешно получен контент через cloudscraper для {url}")
                    return content
        
        logger.warning(f"Cloudscraper не смог получить валидный контент для {url}")
        return None
    except Exception as e:
        logger.error(f"Критическая ошибка cloudscraper для {url}: {e}")
        return None


async def find_rss_feed(base_url: str) -> Optional[str]:
    """Ищет RSS-ленту для сайта (легальный способ получения новостей)"""
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    
    # Стандартные пути для RSS-лент
    rss_paths = [
        '/feed',
        '/rss',
        '/rss.xml',
        '/feed.xml',
        '/news/feed',
        '/news/rss',
        '/blog/feed',
        '/blog/rss',
        '/atom.xml',
        '/index.xml',
    ]
    
    # Также пробуем добавить .rss или /feed к текущему пути
    if '/news' in base_url:
        rss_paths.insert(0, base_url.replace('/news', '/news/feed'))
        rss_paths.insert(1, base_url.replace('/news', '/news/rss'))
        rss_paths.insert(2, base_url + '/feed')
        rss_paths.insert(3, base_url + '/rss')
    
    logger.info(f"Ищу RSS-ленту для {base_url}")
    
    for rss_path in rss_paths:
        if rss_path.startswith('http'):
            rss_url = rss_path
        else:
            rss_url = urljoin(domain, rss_path)
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(rss_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '').lower()
                        content = await response.text()
                        
                        # Проверяем, что это действительно RSS/Atom
                        if ('xml' in content_type or 'rss' in content_type or 
                            'atom' in content_type or 
                            '<rss' in content.lower() or 
                            '<feed' in content.lower() or
                            '<?xml' in content[:100]):
                            logger.info(f"✅ Найдена RSS-лента: {rss_url}")
                            return rss_url
        except Exception as e:
            logger.debug(f"RSS не найден по пути {rss_url}: {e}")
            continue
    
    # Пробуем найти ссылку на RSS в HTML страницы
    try:
        content = await fetch_page_content(base_url)
        if content:
            # Ищем ссылки на RSS в HTML
            rss_patterns = [
                r'<link[^>]*type=["\']application/rss\+xml["\'][^>]*href=["\']([^"\']+)["\']',
                r'<link[^>]*type=["\']application/atom\+xml["\'][^>]*href=["\']([^"\']+)["\']',
                r'<a[^>]*href=["\']([^"\']*feed[^"\']*)["\'][^>]*>',
                r'<a[^>]*href=["\']([^"\']*rss[^"\']*)["\'][^>]*>',
            ]
            
            for pattern in rss_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    rss_url = urljoin(base_url, match)
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(rss_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                                if response.status == 200:
                                    logger.info(f"✅ Найдена RSS-лента в HTML: {rss_url}")
                                    return rss_url
                    except:
                        continue
    except:
        pass
    
    logger.info(f"RSS-лента не найдена для {base_url}")
    return None


async def fetch_rss_content(rss_url: str) -> Optional[Dict]:
    """Получает и парсит RSS-ленту"""
    try:
        logger.info(f"Получаю RSS-ленту: {rss_url}")
        
        # Используем feedparser для парсинга RSS
        def parse_rss(url: str):
            return feedparser.parse(url)
        
        feed = await asyncio.get_event_loop().run_in_executor(executor, parse_rss, rss_url)
        
        if feed.bozo == 0 or len(feed.entries) > 0:  # bozo=0 означает успешный парсинг
            # Формируем список новостей
            items = []
            for entry in feed.entries[:10]:  # Берем последние 10 новостей
                item_id = entry.get('id', entry.get('link', ''))
                title = entry.get('title', 'Без названия')
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))
                
                items.append({
                    'id': item_id,
                    'title': title,
                    'link': link,
                    'published': published
                })
            
            logger.info(f"✅ Получено {len(items)} новостей из RSS")
            return {
                'items': items,
                'feed_title': feed.feed.get('title', 'RSS Feed'),
                'feed_link': feed.feed.get('link', rss_url)
            }
        else:
            logger.warning(f"RSS-лента невалидна или пуста: {rss_url}")
            return None
    except Exception as e:
        logger.error(f"Ошибка при получении RSS: {e}")
        return None


async def fetch_page_content(url: str) -> Optional[str]:
    """Получает содержимое страницы по URL с заголовками браузера для обхода защиты от ботов"""
    # Сначала пробуем через cloudscraper (для Cloudflare)
    try:
        content = await asyncio.get_event_loop().run_in_executor(
            executor, 
            _fetch_with_cloudscraper, 
            url
        )
        if content:
            # Проверяем, не получили ли мы страницу с защитой от ботов
            content_lower = content.lower()
            if any(indicator in content_lower for indicator in [
                'checking your browser', 'ddos protection',
                'please wait', 'just a moment', 'captcha', 'recaptcha'
            ]):
                logger.warning(f"Cloudflare challenge обнаружен на {url}, пробуем обычный метод...")
            else:
                logger.info(f"Успешно получен контент через cloudscraper для {url}")
                return content
    except Exception as e:
        logger.warning(f"Ошибка при использовании cloudscraper для {url}: {e}, пробуем обычный метод...")
    
    # Если cloudscraper не помог, пробуем обычный метод с несколькими вариантами заголовков
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    
    # Извлекаем домен для Referer
    try:
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
    except:
        domain = None
    
    for attempt, user_agent in enumerate(user_agents, 1):
        try:
            # Заголовки реального браузера для обхода защиты от ботов
            headers = {
                'User-Agent': user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate',  # Убрали br, чтобы избежать ошибок, если brotli не установлен
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
            
            # Добавляем Referer, если можем определить домен
            if domain:
                headers['Referer'] = domain
            
            # Создаем сессию с заголовками и поддержкой cookies
            timeout = aiohttp.ClientTimeout(total=45, connect=10)
            connector = aiohttp.TCPConnector(
                limit=100, 
                limit_per_host=30,
                ttl_dns_cache=300,
                force_close=False
            )
            
            # Создаем cookie jar для сохранения cookies между запросами
            cookie_jar = aiohttp.CookieJar(unsafe=True)
            
            async with aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
                connector=connector,
                cookie_jar=cookie_jar
            ) as session:
                # Небольшая задержка перед запросом (имитация человеческого поведения)
                await asyncio.sleep(1 + attempt * 0.5)
                
                # Делаем запрос с поддержкой редиректов
                async with session.get(
                    url, 
                    allow_redirects=True,
                    ssl=False  # Некоторые сайты требуют отключения проверки SSL
                ) as response:
                    if response.status == 200:
                        try:
                            content = await response.text()
                        except Exception as decode_error:
                            # Если ошибка декодирования (например, Brotli не установлен)
                            if 'brotli' in str(decode_error).lower() or 'br' in str(decode_error).lower():
                                logger.warning(f"Ошибка декодирования Brotli для {url}, пробуем без br...")
                                # Пробуем без brotli в заголовках
                                headers_no_br = headers.copy()
                                headers_no_br['Accept-Encoding'] = 'gzip, deflate'
                                # Создаем новую сессию без br
                                async with aiohttp.ClientSession(
                                    headers=headers_no_br,
                                    timeout=timeout,
                                    connector=connector,
                                    cookie_jar=cookie_jar
                                ) as session2:
                                    await asyncio.sleep(1)
                                    async with session2.get(url, allow_redirects=True, ssl=False) as response2:
                                        if response2.status == 200:
                                            content = await response2.text()
                                        else:
                                            if attempt < len(user_agents):
                                                continue
                                            return None
                            else:
                                raise decode_error
                        # Проверяем, не получили ли мы страницу с защитой от ботов
                        content_lower = content.lower()
                        if any(indicator in content_lower for indicator in [
                            'cloudflare', 'checking your browser', 'ddos protection',
                            'please wait', 'just a moment', 'captcha', 'recaptcha'
                        ]):
                            logger.warning(f"Обнаружена защита от ботов на {url}, пробуем другой User-Agent...")
                            if attempt < len(user_agents):
                                continue  # Пробуем следующий User-Agent
                            else:
                                logger.error(f"Не удалось обойти защиту от ботов для {url}")
                                return None
                        return content
                    elif response.status == 403:
                        logger.warning(f"Доступ запрещен (403) для {url}, пробуем другой User-Agent...")
                        if attempt < len(user_agents):
                            continue  # Пробуем следующий User-Agent
                        else:
                            logger.error(f"Доступ запрещен (403) для {url} после всех попыток.")
                            return None
                    elif response.status == 429:
                        # Слишком много запросов - ждем дольше
                        logger.warning(f"Слишком много запросов (429) для {url}, ждем...")
                        await asyncio.sleep(5)
                        if attempt < len(user_agents):
                            continue
                        return None
                    else:
                        logger.error(f"Ошибка при получении страницы {url}: статус {response.status}")
                        if attempt < len(user_agents):
                            continue
                        return None
        except aiohttp.ClientError as e:
            logger.warning(f"Ошибка сети при запросе страницы {url} (попытка {attempt}): {e}")
            if attempt < len(user_agents):
                await asyncio.sleep(2)  # Ждем перед следующей попыткой
                continue
            return None
        except Exception as e:
            logger.error(f"Неожиданная ошибка при запросе страницы {url} (попытка {attempt}): {e}")
            if attempt < len(user_agents):
                await asyncio.sleep(2)
                continue
            return None
    
    return None


def calculate_hash(content: str) -> str:
    """Вычисляет хеш содержимого страницы"""
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def format_interval(minutes: int) -> str:
    """Форматирует интервал в читаемый вид"""
    if minutes < 60:
        return f"{minutes} мин"
    elif minutes < 1440:
        hours = minutes // 60
        return f"{hours} ч"
    else:
        days = minutes // 1440
        return f"{days} дн"


def get_local_time() -> datetime:
    """Возвращает текущее время в часовом поясе UTC+3 (Москва/Минск)"""
    return datetime.now(MOSCOW_TZ)


def format_local_time(dt: Optional[datetime] = None) -> str:
    """Форматирует время в локальном часовом поясе"""
    if dt is None:
        dt = get_local_time()
    elif dt.tzinfo is None:
        # Если время без часового пояса, считаем что это UTC и конвертируем
        dt = dt.replace(tzinfo=timezone.utc).astimezone(MOSCOW_TZ)
    else:
        dt = dt.astimezone(MOSCOW_TZ)
    return dt.strftime('%d.%m.%Y в %H:%M:%S')


async def check_page_changes(chat_id: int, project: Project, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет изменения на странице и отправляет уведомление при изменении"""
    logger.info(f"Начинаю проверку проекта {project.project_id} ({project.name}) для пользователя {chat_id}")
    current_time = get_local_time()
    
    # Сначала пробуем RSS (легальный способ для новостей)
    rss_data = None
    if project.rss_url:
        # Используем уже найденную RSS-ленту
        rss_data = await fetch_rss_content(project.rss_url)
    else:
        # Пытаемся найти RSS-ленту
        rss_url = await find_rss_feed(project.url)
        if rss_url:
            project.rss_url = rss_url
            user_projects[chat_id][project.project_id] = project
            rss_data = await fetch_rss_content(rss_url)
    
    # Если RSS найден и работает - используем его
    if rss_data and rss_data.get('items'):
        items = rss_data['items']
        current_item_ids = [item['id'] for item in items]
        
        if project.last_rss_items is None:
            # Первая проверка - сохраняем список новостей
            project.last_rss_items = current_item_ids
            project.last_check = current_time.isoformat()
            user_projects[chat_id][project.project_id] = project
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Отслеживание через RSS успешно начато!\n\n"
                    f"📌 Проект: {project.name}\n"
                    f"🔗 RSS-лента: {project.rss_url}\n"
                    f"📰 Найдено новостей: {len(items)}\n\n"
                    f"⏰ Периодичность проверки: {format_interval(project.interval_minutes)}\n"
                    f"🔔 Я отправлю уведомление при появлении новых новостей"
                )
            )
            logger.info(f"RSS отслеживание начато для проекта {project.project_id}")
            return
        
        # Проверяем новые новости
        new_items = []
        for item in items:
            if item['id'] not in project.last_rss_items:
                new_items.append(item)
        
        if new_items:
            # Есть новые новости!
            project.last_rss_items = current_item_ids
            project.last_check = current_time.isoformat()
            user_projects[chat_id][project.project_id] = project
            
            for new_item in reversed(new_items):  # От старых к новым
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📰 НОВАЯ НОВОСТЬ!\n\n"
                        f"📌 Проект: {project.name}\n\n"
                        f"📝 {new_item['title']}\n\n"
                        f"🔗 {new_item['link']}\n\n"
                        f"⏰ Время обнаружения: {format_local_time(current_time)}"
                    )
                )
            
            logger.info(f"Обнаружено {len(new_items)} новых новостей для проекта {project.project_id}")
        else:
            # Новых новостей нет
            project.last_check = current_time.isoformat()
            user_projects[chat_id][project.project_id] = project
            
            # Отправляем уведомление, если включено
            should_notify = False
            if project.notify_on_no_changes:
                if project.last_notification:
                    try:
                        last_notif_time = datetime.fromisoformat(project.last_notification)
                        if last_notif_time.tzinfo is None:
                            last_notif_time = last_notif_time.replace(tzinfo=timezone.utc).astimezone(MOSCOW_TZ)
                        else:
                            last_notif_time = last_notif_time.astimezone(MOSCOW_TZ)
                        time_since_last = (current_time - last_notif_time).total_seconds()
                        if time_since_last >= 3600:
                            should_notify = True
                    except:
                        should_notify = True
                else:
                    should_notify = True
            
            if should_notify:
                project.last_notification = current_time.isoformat()
                user_projects[chat_id][project.project_id] = project
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ Проверка RSS выполнена\n\n"
                        f"📌 Проект: {project.name}\n"
                        f"🔗 RSS-лента: {project.rss_url}\n\n"
                        f"⏰ Время проверки: {format_local_time(current_time)}\n"
                        f"📊 Новых новостей: нет\n\n"
                        f"🔄 Следующая проверка через {format_interval(project.interval_minutes)}"
                    )
                )
            
            logger.info(f"Новых новостей не обнаружено для проекта {project.project_id}")
        
        return
    
    # Если RSS не найден или не работает - используем обычный метод
    content = await fetch_page_content(project.url)
    
    if content is None:
        # Обновляем время последней проверки даже при ошибке
        project.last_check = current_time.isoformat()
        user_projects[chat_id][project.project_id] = project
        
        error_message = (
            f"⚠️ Проблема при проверке проекта\n\n"
            f"📌 Проект: {project.name}\n"
            f"🔗 Страница: {project.url}\n\n"
            f"❌ Не удалось получить содержимое страницы.\n"
            f"Возможные причины:\n"
            f"• Страница использует защиту от ботов (Cloudflare, reCAPTCHA и т.д.)\n"
            f"• Страница временно недоступна\n"
            f"• Проблемы с интернет-соединением\n\n"
            f"🔄 Следующая проверка через {format_interval(project.interval_minutes)}"
        )
        await context.bot.send_message(chat_id=chat_id, text=error_message)
        return
    
    current_hash = calculate_hash(content)
    logger.info(f"Хеш страницы для проекта {project.project_id}: {current_hash[:16]}... (предыдущий: {project.last_hash[:16] if project.last_hash else 'нет'}...)")
    
    if project.last_hash is None:
        # Первая проверка - сохраняем хеш
        project.last_hash = current_hash
        project.last_check = current_time.isoformat()
        user_projects[chat_id][project.project_id] = project
        logger.info(f"Первая проверка завершена для проекта {project.project_id}, хеш сохранен")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Отслеживание успешно начато!\n\n"
                f"📌 Проект: {project.name}\n"
                f"🔗 Страница: {project.url}\n\n"
                f"✅ Первая проверка выполнена успешно\n"
                f"📊 Страница сохранена как эталон\n\n"
                f"⏰ Периодичность проверки: {format_interval(project.interval_minutes)}\n"
                f"🔔 Я отправлю уведомление, если обнаружу изменения"
            )
        )
    elif current_hash != project.last_hash:
        # Страница изменилась!
        logger.warning(f"ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ! Проект {project.project_id}: старый хеш {project.last_hash[:16]}..., новый {current_hash[:16]}...")
        project.last_hash = current_hash
        project.last_check = current_time.isoformat()
        user_projects[chat_id][project.project_id] = project
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔔 ВНИМАНИЕ! Обнаружены изменения!\n\n"
                f"📌 Проект: {project.name}\n"
                f"🔗 Страница: {project.url}\n\n"
                f"⏰ Время обнаружения: {format_local_time(current_time)}\n\n"
                f"📝 Страница была изменена. Проверьте её содержимое!"
            )
        )
        logger.info(f"Уведомление об изменениях отправлено для проекта {project.project_id} пользователя {chat_id}")
    else:
        # Изменений нет
        project.last_check = current_time.isoformat()
        user_projects[chat_id][project.project_id] = project
        
        # Отправляем уведомление, если включено, но не чаще раза в час
        should_notify = False
        if project.notify_on_no_changes:
            if project.last_notification:
                try:
                    last_notif_time = datetime.fromisoformat(project.last_notification)
                    if last_notif_time.tzinfo is None:
                        last_notif_time = last_notif_time.replace(tzinfo=timezone.utc).astimezone(MOSCOW_TZ)
                    else:
                        last_notif_time = last_notif_time.astimezone(MOSCOW_TZ)
                    
                    # Отправляем уведомление не чаще раза в час
                    time_since_last = (current_time - last_notif_time).total_seconds()
                    if time_since_last >= 3600:  # 1 час
                        should_notify = True
                except:
                    should_notify = True
            else:
                should_notify = True
        
        if should_notify:
            project.last_notification = current_time.isoformat()
            user_projects[chat_id][project.project_id] = project
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Проверка выполнена\n\n"
                    f"📌 Проект: {project.name}\n"
                    f"🔗 Страница: {project.url}\n\n"
                    f"⏰ Время проверки: {format_local_time(current_time)}\n"
                    f"📊 Изменений не обнаружено\n\n"
                    f"🔄 Следующая проверка через {format_interval(project.interval_minutes)}"
                )
            )
            logger.info(f"Отправлено уведомление об отсутствии изменений для проекта {project.project_id}")
        
        logger.info(f"Изменений не обнаружено для проекта {project.project_id}, следующая проверка через {format_interval(project.interval_minutes)}")


async def monitoring_loop(chat_id: int, project: Project, context: ContextTypes.DEFAULT_TYPE):
    """Основной цикл мониторинга страницы"""
    task_key = (chat_id, project.project_id)
    logger.info(f"Запущен цикл мониторинга для проекта {project.project_id} пользователя {chat_id}, интервал: {project.interval_minutes} минут")
    
    while (chat_id in user_projects and 
           project.project_id in user_projects[chat_id] and 
           user_projects[chat_id][project.project_id].is_active):
        try:
            # Получаем актуальную версию проекта (на случай изменения настроек)
            current_project = user_projects[chat_id][project.project_id]
            interval_seconds = current_project.interval_minutes * 60
            
            # Выполняем проверку
            await check_page_changes(chat_id, current_project, context)
            
            # Логируем время следующей проверки
            next_check_time = get_local_time() + timedelta(seconds=interval_seconds)
            logger.info(f"Проверка завершена для проекта {current_project.project_id}, следующая проверка в {format_local_time(next_check_time)}")
            
            # Ждем указанный интервал
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info(f"Мониторинг остановлен для проекта {project.project_id} пользователя {chat_id}")
            break
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга для проекта {project.project_id}: {e}")
            await asyncio.sleep(60)  # Ждем минуту перед повторной попыткой
    
    # Удаляем задачу из словаря
    if task_key in monitoring_tasks:
        del monitoring_tasks[task_key]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    
    # Инициализируем список проектов для пользователя, если его еще нет
    if chat_id not in user_projects:
        user_projects[chat_id] = {}
    
    welcome_message = (
        "👋 Привет! Я бот для отслеживания изменений на веб-страницах.\n\n"
        "📋 Возможности:\n"
        "• Отслеживание нескольких страниц одновременно\n"
        "• Настройка периодичности проверки для каждого проекта\n"
        "• Управление проектами через удобное меню\n\n"
        "📌 Доступные команды:\n"
        "/list - показать все проекты\n"
        "/add <ссылка> - добавить новый проект\n"
        "/delete <номер> - удалить проект\n"
        "/interval <номер> <минуты> - изменить периодичность\n"
        "/status <номер> - статус проекта\n"
        "/menu - открыть меню управления\n\n"
        "💡 Просто отправьте ссылку, чтобы быстро добавить проект!"
    )
    
    await update.message.reply_text(welcome_message)
    await show_projects_menu(update, context)


async def show_projects_menu(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    """Показывает меню со списком всех проектов"""
    chat_id = update.effective_chat.id
    
    if chat_id not in user_projects or not user_projects[chat_id]:
        keyboard = [[InlineKeyboardButton("➕ Добавить проект", callback_data="add_project")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = "📋 У вас пока нет активных проектов.\n\nНажмите кнопку ниже, чтобы добавить первый проект."
        
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        return
    
    projects = user_projects[chat_id]
    text = "📋 Ваши проекты отслеживания:\n\n"
    
    keyboard = []
    for idx, (project_id, project) in enumerate(projects.items(), 1):
        status_icon = "✅" if project.is_active else "⏸"
        last_check = "Ещё не проверялась"
        if project.last_check:
            try:
                check_time = datetime.fromisoformat(project.last_check)
                last_check = format_local_time(check_time)
            except:
                pass
        
        # Обрезаем длинные URL для отображения
        display_url = project.url[:50] + "..." if len(project.url) > 50 else project.url
        
        text += (
            f"{idx}. {status_icon} {project.name}\n"
            f"   🔗 {display_url}\n"
            f"   ⏰ Проверка: {format_interval(project.interval_minutes)} | Последняя: {last_check}\n\n"
        )
        
        # Кнопки для каждого проекта
        keyboard.append([
            InlineKeyboardButton(f"⚙️ {idx}", callback_data=f"project_{project_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{project_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Добавить проект", callback_data="add_project")])
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="refresh_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-запросов от кнопок"""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.from_user.id
    data = query.data
    
    if data == "add_project":
        await query.edit_message_text(
            "➕ Добавление нового проекта\n\n"
            "Отправьте ссылку на страницу, которую хотите отслеживать.\n\n"
            "Пример: https://example.com"
        )
    elif data == "refresh_menu":
        await show_projects_menu(update, context)
    elif data.startswith("project_"):
        project_id = data.split("_", 1)[1]
        await show_project_details(chat_id, project_id, query)
    elif data.startswith("delete_"):
        project_id = data.split("_", 1)[1]
        await delete_project(chat_id, project_id, query)
    elif data.startswith("interval_"):
        parts = data.split("_")
        project_id = parts[1]
        minutes = int(parts[2])
        await set_interval(chat_id, project_id, minutes, query)
    elif data.startswith("toggle_"):
        project_id = data.split("_", 1)[1]
        await toggle_project(chat_id, project_id, query)
    elif data.startswith("notify_"):
        project_id = data.split("_", 1)[1]
        await toggle_notifications(chat_id, project_id, query)
    elif data.startswith("notify_"):
        project_id = data.split("_", 1)[1]
        await toggle_notifications(chat_id, project_id, query)


async def show_project_details(chat_id: int, project_id: str, query):
    """Показывает детали проекта и кнопки управления"""
    if chat_id not in user_projects or project_id not in user_projects[chat_id]:
        await query.edit_message_text("❌ Проект не найден.")
        return
    
    project = user_projects[chat_id][project_id]
    
    last_check = "Ещё не проверялась"
    if project.last_check:
        try:
            check_time = datetime.fromisoformat(project.last_check)
            last_check = format_local_time(check_time)
        except:
            pass
    
    status_text = "✅ Активен" if project.is_active else "⏸ Остановлен"
    
    text = (
        f"📌 Проект: {project.name}\n\n"
        f"🔗 URL: {project.url}\n"
        f"⏰ Периодичность: {format_interval(project.interval_minutes)}\n"
        f"📊 Статус: {status_text}\n"
        f"🕐 Последняя проверка: {last_check}\n"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("⏰ 15 мин", callback_data=f"interval_{project_id}_15"),
            InlineKeyboardButton("⏰ 30 мин", callback_data=f"interval_{project_id}_30"),
            InlineKeyboardButton("⏰ 1 час", callback_data=f"interval_{project_id}_60")
        ],
        [
            InlineKeyboardButton("⏰ 3 часа", callback_data=f"interval_{project_id}_180"),
            InlineKeyboardButton("⏰ 6 часов", callback_data=f"interval_{project_id}_360"),
            InlineKeyboardButton("⏰ 12 часов", callback_data=f"interval_{project_id}_720")
        ],
        [
            InlineKeyboardButton("⏰ 24 часа", callback_data=f"interval_{project_id}_1440"),
        ],
        [
            InlineKeyboardButton("⏸ Остановить" if project.is_active else "▶️ Запустить", 
                               callback_data=f"toggle_{project_id}")
        ],
        [
            InlineKeyboardButton(
                "🔔 Уведомления: ВКЛ" if project.notify_on_no_changes else "🔕 Уведомления: ВЫКЛ",
                callback_data=f"notify_{project_id}"
            )
        ],
        [
            InlineKeyboardButton("🔙 Назад к списку", callback_data="refresh_menu")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)


async def delete_project(chat_id: int, project_id: str, query):
    """Удаляет проект"""
    if chat_id not in user_projects or project_id not in user_projects[chat_id]:
        await query.edit_message_text("❌ Проект не найден.")
        return
    
    project = user_projects[chat_id][project_id]
    
    # Останавливаем задачу мониторинга
    task_key = (chat_id, project_id)
    if task_key in monitoring_tasks:
        monitoring_tasks[task_key].cancel()
        del monitoring_tasks[task_key]
    
    # Удаляем проект
    del user_projects[chat_id][project_id]
    
    await query.edit_message_text(
        f"✅ Проект '{project.name}' удалён.\n\n"
        f"🔗 URL: {project.url}"
    )
    
    # Показываем обновлённое меню через секунду
    await asyncio.sleep(1)
    fake_update = Update(update_id=query.update_id, callback_query=query)
    await show_projects_menu(fake_update)


async def set_interval(chat_id: int, project_id: str, minutes: int, query):
    """Устанавливает интервал проверки для проекта"""
    if chat_id not in user_projects or project_id not in user_projects[chat_id]:
        await query.edit_message_text("❌ Проект не найден.")
        return
    
    project = user_projects[chat_id][project_id]
    project.interval_minutes = minutes
    user_projects[chat_id][project_id] = project
    
    await query.answer(f"✅ Периодичность изменена на {format_interval(minutes)}")
    await show_project_details(chat_id, project_id, query)


async def toggle_project(chat_id: int, project_id: str, query):
    """Включает/выключает проект"""
    if chat_id not in user_projects or project_id not in user_projects[chat_id]:
        await query.edit_message_text("❌ Проект не найден.")
        return
    
    project = user_projects[chat_id][project_id]
    project.is_active = not project.is_active
    user_projects[chat_id][project_id] = project
    
    if project.is_active:
        # Запускаем мониторинг
        task = asyncio.create_task(monitoring_loop(chat_id, project, query.bot))
        monitoring_tasks[(chat_id, project_id)] = task
        await query.answer("✅ Проект запущен")
    else:
        # Останавливаем мониторинг
        task_key = (chat_id, project_id)
        if task_key in monitoring_tasks:
            monitoring_tasks[task_key].cancel()
            del monitoring_tasks[task_key]
        await query.answer("⏸ Проект остановлен")
    
    await show_project_details(chat_id, project_id, query)


async def toggle_notifications(chat_id: int, project_id: str, query):
    """Включает/выключает уведомления об отсутствии изменений"""
    if chat_id not in user_projects or project_id not in user_projects[chat_id]:
        await query.edit_message_text("❌ Проект не найден.")
        return
    
    project = user_projects[chat_id][project_id]
    project.notify_on_no_changes = not project.notify_on_no_changes
    user_projects[chat_id][project_id] = project
    
    status = "включены" if project.notify_on_no_changes else "выключены"
    await query.answer(f"🔔 Уведомления {status}")
    await show_project_details(chat_id, project_id, query)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сообщений с URL для быстрого добавления проекта"""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # Простая проверка на URL
    if not (text.startswith('http://') or text.startswith('https://')):
        await update.message.reply_text(
            "❌ Ошибка: некорректная ссылка!\n\n"
            "Пожалуйста, отправьте ссылку, которая начинается с:\n"
            "• http://\n"
            "• https://\n\n"
            "Пример: https://example.com"
        )
        return
    
    # Инициализируем список проектов, если его еще нет
    if chat_id not in user_projects:
        user_projects[chat_id] = {}
    
    # Создаем новый проект
    project_id = str(uuid.uuid4())[:8]
    project_name = text.split('/')[-1] if text.split('/')[-1] else text.split('/')[-2]
    if not project_name or len(project_name) > 50:
        project_name = f"Проект {len(user_projects[chat_id]) + 1}"
    
    project = Project(
        project_id=project_id,
        url=text,
        name=project_name,
        interval_minutes=60,
        is_active=True,
        notify_on_no_changes=False,  # По умолчанию выключено, можно включить в настройках
        rss_url=None,  # Будет найдена автоматически при первой проверке
        last_rss_items=None
    )
    
    user_projects[chat_id][project_id] = project
    
    await update.message.reply_text(
        f"✅ Проект добавлен!\n\n"
        f"📌 Название: {project.name}\n"
        f"🔗 URL: {text}\n"
        f"⏰ Периодичность: {format_interval(project.interval_minutes)}\n\n"
        f"🔄 Выполняю первую проверку..."
    )
    
    # Запускаем мониторинг
    task = asyncio.create_task(monitoring_loop(chat_id, project, context))
    monitoring_tasks[(chat_id, project_id)] = task
    
    # Выполняем первую проверку сразу
    await check_page_changes(chat_id, project, context)
    
    # Показываем меню
    await show_projects_menu(update, context)


async def list_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /list"""
    await show_projects_menu(update, context)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /menu"""
    await show_projects_menu(update, context)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /delete <номер>"""
    chat_id = update.effective_chat.id
    
    if chat_id not in user_projects or not user_projects[chat_id]:
        await update.message.reply_text("❌ У вас нет активных проектов.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите номер проекта для удаления.\n\n"
            "Использование: /delete <номер>\n"
            "Пример: /delete 1\n\n"
            "Или используйте /menu для управления через кнопки."
        )
        return
    
    try:
        project_num = int(context.args[0])
        projects_list = list(user_projects[chat_id].items())
        
        if project_num < 1 or project_num > len(projects_list):
            await update.message.reply_text(f"❌ Проект с номером {project_num} не найден.")
            return
        
        project_id, project = projects_list[project_num - 1]
        
        # Останавливаем задачу мониторинга
        task_key = (chat_id, project_id)
        if task_key in monitoring_tasks:
            monitoring_tasks[task_key].cancel()
            del monitoring_tasks[task_key]
        
        # Удаляем проект
        del user_projects[chat_id][project_id]
        
        await update.message.reply_text(
            f"✅ Проект '{project.name}' удалён.\n\n"
            f"🔗 URL: {project.url}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Номер проекта должен быть числом.")


async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /interval <номер> <минуты>"""
    chat_id = update.effective_chat.id
    
    if chat_id not in user_projects or not user_projects[chat_id]:
        await update.message.reply_text("❌ У вас нет активных проектов.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Укажите номер проекта и интервал в минутах.\n\n"
            "Использование: /interval <номер> <минуты>\n"
            "Пример: /interval 1 30\n\n"
            "Или используйте /menu для управления через кнопки."
        )
        return
    
    try:
        project_num = int(context.args[0])
        minutes = int(context.args[1])
        
        if minutes < 1:
            await update.message.reply_text("❌ Интервал должен быть больше 0 минут.")
            return
        
        projects_list = list(user_projects[chat_id].items())
        
        if project_num < 1 or project_num > len(projects_list):
            await update.message.reply_text(f"❌ Проект с номером {project_num} не найден.")
            return
        
        project_id, project = projects_list[project_num - 1]
        project.interval_minutes = minutes
        user_projects[chat_id][project_id] = project
        
        await update.message.reply_text(
            f"✅ Периодичность проверки для проекта '{project.name}' изменена на {format_interval(minutes)}."
        )
        
    except ValueError:
        await update.message.reply_text("❌ Номер проекта и интервал должны быть числами.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status <номер>"""
    chat_id = update.effective_chat.id
    
    if chat_id not in user_projects or not user_projects[chat_id]:
        await update.message.reply_text("❌ У вас нет активных проектов.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Укажите номер проекта.\n\n"
            "Использование: /status <номер>\n"
            "Пример: /status 1\n\n"
            "Или используйте /menu для просмотра всех проектов."
        )
        return
    
    try:
        project_num = int(context.args[0])
        projects_list = list(user_projects[chat_id].items())
        
        if project_num < 1 or project_num > len(projects_list):
            await update.message.reply_text(f"❌ Проект с номером {project_num} не найден.")
            return
        
        project_id, project = projects_list[project_num - 1]
        
        last_check = "Ещё не проверялась"
        if project.last_check:
            try:
                check_time = datetime.fromisoformat(project.last_check)
                last_check = format_local_time(check_time)
            except:
                pass
        
        status_text = "✅ Активен" if project.is_active else "⏸ Остановлен"
        
        text = (
            f"📊 Статус проекта:\n\n"
            f"📌 Название: {project.name}\n"
            f"🔗 URL: {project.url}\n"
            f"⏰ Периодичность: {format_interval(project.interval_minutes)}\n"
            f"📊 Статус: {status_text}\n"
            f"🕐 Последняя проверка: {last_check}\n"
        )
        
        await update.message.reply_text(text)
        
    except ValueError:
        await update.message.reply_text("❌ Номер проекта должен быть числом.")


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
    application.add_handler(CommandHandler("list", list_projects))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("interval", interval_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    # Запускаем бота
    logger.info("Бот запущен...")
    print("Бот запущен! Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
