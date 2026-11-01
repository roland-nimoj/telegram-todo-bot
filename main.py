"""
Telegram-бот «Todo-List» на aiogram 3 и SQLite.

Назначение файла:
    Единая точка входа для домашнего задания: хранение задач пользователей,
    вывод списка и выгрузка в CSV.

Как запустить:
    1. Вставьте токен в BOT_TOKEN ниже (или задайте переменную окружения BOT_TOKEN).
    2. pip install -r requirements.txt
    3. python main.py
"""

import asyncio
import csv
import io
import logging
import os
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# Токен бота: вставьте свой токен от @BotFather вместо плейсхолдера.
# Если задана переменная окружения BOT_TOKEN, она имеет приоритет.
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА")

# Имя файла SQLite. Файл создаётся рядом со скриптом при первом запуске.
DB_PATH = "tasks.db"

# Настройка логов, чтобы в консоли было видно ошибки и входящие апдейты.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Роутер собирает все хендлеры; позже его подключаем к Dispatcher.
router = Router()


# ---------------------------------------------------------------------------
# FSM: состояния диалога при добавлении задачи
# ---------------------------------------------------------------------------

class AddTaskStates(StatesGroup):
    """Состояния FSM для команды /add.

    waiting_for_task — бот ждёт текст новой задачи от пользователя.
    """

    waiting_for_task = State()


# ---------------------------------------------------------------------------
# Работа с базой данных (aiosqlite — асинхронный доступ к SQLite)
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Создаёт таблицу tasks, если её ещё нет.

    Зачем: при каждом старте бота схема должна существовать,
    но повторное CREATE TABLE без IF NOT EXISTS сломает запуск.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                user TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()
        logger.info("Таблица tasks готова к работе.")


async def add_task(text: str, user: str) -> int:
    """Сохраняет задачу в БД и возвращает её id.

    Параметры:
        text — текст задачи (уже проверен, что не пустой).
        user — отображаемое имя пользователя Telegram (username или full_name).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (text, user) VALUES (?, ?)",
            (text, user),
        )
        await db.commit()
        # lastrowid — id только что вставленной строки (AUTOINCREMENT).
        return cursor.lastrowid


async def get_all_tasks() -> list[tuple]:
    """Возвращает все задачи, новые сверху.

    Формат строки: (id, text, user, created_at).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, text, user, created_at FROM tasks ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        return [tuple(row) for row in rows]


def format_created_at(value) -> str:
    """Приводит дату из SQLite к читаемому виду ГГГГ-ММ-ДД ЧЧ:ММ:СС.

    SQLite может отдать TIMESTAMP как строку. Эта функция нужна,
    чтобы вывод в /list и CSV был одинаковым и аккуратным.
    """
    if value is None:
        return "не указана"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).replace("T", " ")
    return text[:19] if len(text) >= 19 else text


def user_label(from_user: types.User) -> str:
    """Собирает подпись автора задачи для колонки user.

    Берём @username, если он есть; иначе имя и фамилию.
    Так в списке понятно, кто добавил задачу.
    """
    if from_user.username:
        return f"@{from_user.username}"
    full_name = from_user.full_name.strip()
    return full_name or str(from_user.id)


# ---------------------------------------------------------------------------
# Хендлеры команд
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает /start: приветствие и краткая инструкция.

    Дополнительно сбрасываем FSM, чтобы «зависшее» ожидание текста
    после /add не мешало обычным командам.
    """
    await state.clear()
    await message.answer(
        "Привет! Я бот для хранения задач (Todo-List).\n\n"
        "Доступные команды:\n"
        "/add — добавить новую задачу\n"
        "/list — показать все задачи\n"
        "/list_csv — скачать задачи в CSV-файле\n\n"
        "Начните с /add, чтобы сохранить первую задачу."
    )


@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает /add: переводит пользователя в состояние waiting_for_task.

    Саму запись в БД делаем не здесь, а в следующем сообщении,
    когда пользователь пришлёт текст задачи.
    """
    await state.set_state(AddTaskStates.waiting_for_task)
    await message.answer(
        "Введите текст задачи одним сообщением.\n"
        "Чтобы отменить добавление, отправьте /cancel."
    )


@router.message(Command("cancel"), AddTaskStates.waiting_for_task)
async def cmd_cancel_add(message: types.Message, state: FSMContext) -> None:
    """Отменяет добавление задачи и выходит из FSM.

    Команда не требовалась в задании, но без неё пользователь
    может «застрять» в ожидании текста.
    """
    await state.clear()
    await message.answer("Добавление задачи отменено.")


@router.message(Command("list"))
async def cmd_list(message: types.Message) -> None:
    """Выводит все задачи из БД в заданном формате.

    Формат строки: [ID] Текст (Добавил: user, Дата: created_at).
    Telegram ограничивает длину сообщения ~4096 символов,
    поэтому длинный список разбиваем на несколько сообщений.
    """
    tasks = await get_all_tasks()
    if not tasks:
        await message.answer("Список задач пуст. Добавьте первую через /add.")
        return

    lines = [
        f"[{task_id}] {text} (Добавил: {user}, Дата: {format_created_at(created_at)})"
        for task_id, text, user, created_at in tasks
    ]

    # Собираем чанки, чтобы не превысить лимит длины сообщения Telegram.
    chunk: list[str] = []
    chunk_len = 0
    header = "Список задач:\n\n"

    async def send_chunk(parts: list[str]) -> None:
        await message.answer(header + "\n".join(parts))

    for line in lines:
        extra = len(line) + 1  # +1 за перевод строки
        if chunk and chunk_len + extra > 3500:
            await send_chunk(chunk)
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += extra

    if chunk:
        await send_chunk(chunk)


@router.message(Command("list_csv"))
async def cmd_list_csv(message: types.Message) -> None:
    """Генерирует CSV с колонками ID, Задача, Пользователь, Дата и шлёт файл.

    BufferedInputFile позволяет отдать файл из памяти, без записи на диск.
    Кодировка utf-8-sig удобна для открытия в Excel (BOM для кириллицы).
    """
    tasks = await get_all_tasks()
    if not tasks:
        await message.answer("Список задач пуст, CSV формировать нечего.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["ID", "Задача", "Пользователь", "Дата"])
    for task_id, text, user, created_at in tasks:
        writer.writerow([task_id, text, user, format_created_at(created_at)])

    csv_bytes = buffer.getvalue().encode("utf-8-sig")
    document = BufferedInputFile(
        csv_bytes,
        filename="tasks.csv",
    )
    await message.answer_document(
        document=document,
        caption="Выгрузка задач в CSV.",
    )


@router.message(AddTaskStates.waiting_for_task, F.text)
async def process_task_text(message: types.Message, state: FSMContext) -> None:
    """Принимает текст задачи в состоянии waiting_for_task и пишет его в БД.

    Фильтр F.text нужен, чтобы стикеры/фото не попадали в колонку text.
    Команды обрабатываются хендлерами выше (они зарегистрированы раньше),
    поэтому сюда попадает обычный текст задачи.
    """
    text = (message.text or "").strip()

    if not text:
        await message.answer("Текст задачи не может быть пустым. Попробуйте ещё раз.")
        return

    task_id = await add_task(text=text, user=user_label(message.from_user))
    await state.clear()
    await message.answer(f"Задача сохранена. ID: {task_id}\n\n{text}")


@router.message(AddTaskStates.waiting_for_task)
async def process_task_non_text(message: types.Message) -> None:
    """Реакция на не-текст в состоянии waiting_for_task.

    Нужен отдельный хендлер, чтобы пользователь понял,
    что бот ждёт именно текстовое описание.
    """
    await message.answer("Нужен обычный текст задачи, а не файл или стикер. Напишите задачу словами.")


@router.message(F.text.startswith("/"))
async def unknown_command(message: types.Message) -> None:
    """Подсказка, если пользователь ввёл неизвестную команду."""
    await message.answer(
        "Неизвестная команда. Используйте /start, /add, /list или /list_csv."
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    """Запускает бота: готовит БД, создаёт Bot/Dispatcher и начинает polling.

    MemoryStorage хранит FSM в оперативной памяти — для домашки достаточно.
    После перезапуска незавершённый /add придётся начать заново.
    """
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА":
        raise RuntimeError(
            "Не задан токен бота. Вставьте его в BOT_TOKEN в начале main.py "
            "или установите переменную окружения BOT_TOKEN."
        )

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен. Ожидаю сообщения...")
    # drop_pending_updates=True — не обрабатывать старые апдейты, накопленные офлайн.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    # asyncio.run нужен, потому что aiogram 3 полностью асинхронный.
    asyncio.run(main())
