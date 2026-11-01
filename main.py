"""
Telegram-бот «Todo-List» на aiogram 3 и SQLite.

Назначение файла:
    Единая точка входа для домашнего задания: хранение задач пользователей,
    вывод списка и выгрузка в CSV.

Как запустить:
    1. Скопируйте .env.example в .env и вставьте токен от @BotFather.
    2. pip install -r requirements.txt
    3. python main.py
"""

import asyncio
import csv
import io
import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# Путь к секретному файлу с токеном. Он в .gitignore и в git не попадает.
ENV_PATH = Path(__file__).parent / ".env"


def load_env_file(path: Path = ENV_PATH) -> None:
    """Читает KEY=VALUE из .env и кладёт значения в os.environ.

    Нужен, чтобы хранить BOT_TOKEN вне main.py и не коммитить секрет.
    Уже заданные переменные окружения не перезаписываем:
    так можно запускать бота с export BOT_TOKEN=... без правки файла.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


# Сначала подхватываем .env, затем читаем токен из окружения.
load_env_file()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Имя файла SQLite. Файл создаётся рядом со скриптом при первом запуске.
DB_PATH = "tasks.db"

# Значения по умолчанию для новых столбцов (и для старых записей после миграции).
DEFAULT_STATUS = "В работе"
DEFAULT_CATEGORY = "Общее"
DONE_STATUS = "Выполнено"

# Подсказки категорий на клавиатуре. Любой другой текст тоже сохранится как категория.
SUGGESTED_CATEGORIES = ("Работа", "Дом", "Учеба")

# Подписи кнопок постоянного меню. Нажатие отправляет этот текст боту.
MENU_BUTTON = "меню/start"
DONE_BUTTON = "/done <id>"

# Текст приветствия: его показывают /start, кнопка меню и обычные сообщения.
WELCOME_TEXT = (
    "Привет! Я бот для хранения задач (Todo-List).\n\n"
    "Доступные команды:\n"
    "/add — добавить новую задачу\n"
    "/list — показать все задачи\n"
    "/list_csv — скачать задачи в CSV-файле\n"
    "/done — отметить задачу выполненной\n\n"
    "Команды также доступны кнопками меню внизу экрана.\n"
    "Начните с /add, чтобы сохранить первую задачу."
)

# Настройка логов, чтобы в консоли было видно ошибки и входящие апдейты.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Роутер собирает все хендлеры; позже его подключаем к Dispatcher.
router = Router()


# ---------------------------------------------------------------------------
# FSM: состояния диалога
# ---------------------------------------------------------------------------

class AddTaskStates(StatesGroup):
    """Состояния FSM для команды /add.

    waiting_for_task — бот ждёт текст новой задачи.
    waiting_for_category — текст уже есть, ждём категорию.
    """

    waiting_for_task = State()
    waiting_for_category = State()


class DoneTaskStates(StatesGroup):
    """Состояние FSM для /done без номера задачи.

    waiting_for_id — бот ждёт, пока пользователь пришлёт id.
    """

    waiting_for_id = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянные кнопки меню. Каждая кнопка дублирует соответствующую команду."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=MENU_BUTTON),
                KeyboardButton(text="/add"),
            ],
            [
                KeyboardButton(text="/list"),
                KeyboardButton(text="/list_csv"),
            ],
            [
                KeyboardButton(text=DONE_BUTTON),
            ],
        ],
        resize_keyboard=True,
    )


def category_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с типовыми категориями.

    Пользователь может нажать кнопку или ввести свою категорию текстом.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=name) for name in SUGGESTED_CATEGORIES]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def send_welcome(message: types.Message) -> None:
    """Отправляет стартовое приветствие и показывает кнопки меню."""
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Работа с базой данных (aiosqlite — асинхронный доступ к SQLite)
# ---------------------------------------------------------------------------

async def _table_columns(db: aiosqlite.Connection) -> set[str]:
    """Возвращает имена столбцов таблицы tasks.

    PRAGMA table_info нужен для миграции: так мы не пытаемся
    повторно добавить уже существующий столбец.
    """
    cursor = await db.execute("PRAGMA table_info(tasks)")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def init_db() -> None:
    """Создаёт таблицу tasks и дописывает новые столбцы, если их ещё нет.

    CREATE TABLE IF NOT EXISTS покрывает первый запуск.
    ALTER TABLE нужен для уже существующей базы без status/category:
    старые задачи не теряются, им проставляются значения по умолчанию.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                user TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'В работе',
                category TEXT DEFAULT 'Общее'
            )
            """
        )

        columns = await _table_columns(db)
        if "status" not in columns:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'В работе'"
            )
            logger.info("Миграция: добавлен столбец status.")
        if "category" not in columns:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT 'Общее'"
            )
            logger.info("Миграция: добавлен столбец category.")

        # На всякий случай заполняем пустые значения у старых строк.
        await db.execute(
            "UPDATE tasks SET status = ? WHERE status IS NULL OR TRIM(status) = ''",
            (DEFAULT_STATUS,),
        )
        await db.execute(
            "UPDATE tasks SET category = ? WHERE category IS NULL OR TRIM(category) = ''",
            (DEFAULT_CATEGORY,),
        )
        await db.commit()
        logger.info("Таблица tasks готова к работе.")


async def add_task(text: str, user: str, category: str) -> int:
    """Сохраняет задачу в БД и возвращает её id.

    Параметры:
        text — текст задачи (уже проверен, что не пустой).
        user — отображаемое имя пользователя Telegram (username или full_name).
        category — категория, которую указал пользователь.
    Статус всегда стартует как «В работе».
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (text, user, status, category) VALUES (?, ?, ?, ?)",
            (text, user, DEFAULT_STATUS, category),
        )
        await db.commit()
        # lastrowid — id только что вставленной строки (AUTOINCREMENT).
        return cursor.lastrowid


async def get_all_tasks() -> list[tuple]:
    """Возвращает все задачи по возрастанию id.

    Формат строки: (id, text, user, created_at, status, category).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, text, user, created_at, status, category
            FROM tasks
            ORDER BY id ASC
            """
        )
        rows = await cursor.fetchall()
        return [tuple(row) for row in rows]


async def mark_task_done(task_id: int) -> tuple | None:
    """Ставит задаче статус «Выполнено».

    Возвращает строку задачи после обновления или None, если id не найден.
    Так /done может и подтвердить успех, и сказать, что задачи нет.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, text, status, category FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        await db.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (DONE_STATUS, task_id),
        )
        await db.commit()
        return (row["id"], row["text"], DONE_STATUS, row["category"])


def format_created_at(value) -> str:
    """Приводит дату из SQLite к читаемому виду ГГГГ-ММ-ДД ЧЧ:ММ:СС.

    SQLite может отдать TIMESTAMP как строку. Эта функция нужна,
    чтобы вывод в CSV был одинаковым и аккуратным.
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


def parse_task_id(raw: str | None) -> int | None:
    """Достаёт числовой id задачи из текста.

    Принимает «1» или «/done 1». Плейсхолдер «<id>» с кнопки меню не считается номером.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or text in {"<id>", DONE_BUTTON}:
        return None
    if text.isdigit():
        return int(text)
    return None


async def finish_done(message: types.Message, task_id: int, state: FSMContext) -> None:
    """Помечает задачу выполненной и отвечает пользователю."""
    task = await mark_task_done(task_id)
    await state.clear()
    if task is None:
        await message.answer(
            f"Задача с ID {task_id} не найдена. Проверьте номер в /list.",
            reply_markup=main_menu_keyboard(),
        )
        return

    saved_id, text, status, category = task
    await message.answer(
        f"Задача [{saved_id}] отмечена как «{status}».\n"
        f"{text} (Категория: {category})",
        reply_markup=main_menu_keyboard(),
    )


async def ask_done_id(message: types.Message, state: FSMContext) -> None:
    """Просит номер задачи, если /done вызвали без id или нажали кнопку меню."""
    await state.set_state(DoneTaskStates.waiting_for_id)
    await message.answer(
        "Введите номер задания, которое выполнено.\n"
        "Номер можно посмотреть в /list. Для отмены отправьте /cancel.",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Хендлеры команд и кнопок меню
# ---------------------------------------------------------------------------

@router.message(CommandStart())
@router.message(F.text == MENU_BUTTON)
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает /start и кнопку «меню/start».

    Сбрасываем FSM и показываем приветствие со списком команд.
    """
    await state.clear()
    await send_welcome(message)


@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext) -> None:
    """Обрабатывает /add и кнопку /add: ждём текст задачи."""
    await state.set_state(AddTaskStates.waiting_for_task)
    await message.answer(
        "Введите текст задачи одним сообщением.\n"
        "Чтобы отменить добавление, отправьте /cancel.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(
    Command("cancel"),
    StateFilter(
        AddTaskStates.waiting_for_task,
        AddTaskStates.waiting_for_category,
        DoneTaskStates.waiting_for_id,
    ),
)
async def cmd_cancel(message: types.Message, state: FSMContext) -> None:
    """Отменяет текущий диалог (/add или /done) и возвращает меню."""
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())


@router.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext) -> None:
    """Выводит все задачи из БД в заданном формате.

    Формат строки: [ID] Текст (Категория: ..., Статус: ...).
    Telegram ограничивает длину сообщения ~4096 символов,
    поэтому длинный список разбиваем на несколько сообщений.
    """
    await state.clear()
    tasks = await get_all_tasks()
    if not tasks:
        await message.answer(
            "Список задач пуст. Добавьте первую через /add.",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = [
        f"[{task_id}] {text} (Категория: {category}, Статус: {status})"
        for task_id, text, _user, _created_at, status, category in tasks
    ]

    # Собираем чанки, чтобы не превысить лимит длины сообщения Telegram.
    chunk: list[str] = []
    chunk_len = 0
    header = "Список задач:\n\n"

    async def send_chunk(parts: list[str]) -> None:
        await message.answer(
            header + "\n".join(parts),
            reply_markup=main_menu_keyboard(),
        )

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
async def cmd_list_csv(message: types.Message, state: FSMContext) -> None:
    """Генерирует CSV и отправляет его документом.

    Колонки: ID, Задача, Пользователь, Дата, Статус, Категория.
    BufferedInputFile позволяет отдать файл из памяти, без записи на диск.
    Кодировка utf-8-sig удобна для открытия в Excel (BOM для кириллицы).
    """
    await state.clear()
    tasks = await get_all_tasks()
    if not tasks:
        await message.answer(
            "Список задач пуст, CSV формировать нечего.",
            reply_markup=main_menu_keyboard(),
        )
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["ID", "Задача", "Пользователь", "Дата", "Статус", "Категория"])
    for task_id, text, user, created_at, status, category in tasks:
        writer.writerow(
            [
                task_id,
                text,
                user,
                format_created_at(created_at),
                status or DEFAULT_STATUS,
                category or DEFAULT_CATEGORY,
            ]
        )

    csv_bytes = buffer.getvalue().encode("utf-8-sig")
    document = BufferedInputFile(
        csv_bytes,
        filename="tasks.csv",
    )
    await message.answer_document(
        document=document,
        caption="Выгрузка задач в CSV.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("done"))
async def cmd_done(message: types.Message, state: FSMContext, command: CommandObject) -> None:
    """Отмечает задачу выполненной.

    /done 1 — сразу обновляет статус.
    /done без номера или кнопка «/done <id>» — спрашиваем номер отдельным шагом.
    Кнопка меню отправляет текст «/done <id>», и Telegram разбирает это как команду done.
    """
    task_id = parse_task_id(command.args)
    if task_id is None:
        await ask_done_id(message, state)
        return
    await finish_done(message, task_id, state)


@router.message(DoneTaskStates.waiting_for_id, F.text)
async def process_done_id(message: types.Message, state: FSMContext) -> None:
    """Принимает номер задачи после запроса и ставит статус «Выполнено»."""
    task_id = parse_task_id(message.text)
    if task_id is None:
        await message.answer(
            "Нужен номер задачи цифрами. Пример: 1\n"
            "Посмотреть номера можно в /list. Отмена: /cancel.",
            reply_markup=main_menu_keyboard(),
        )
        return
    await finish_done(message, task_id, state)


@router.message(DoneTaskStates.waiting_for_id)
async def process_done_id_non_text(message: types.Message) -> None:
    """Если вместо номера прислали файл или стикер."""
    await message.answer(
        "Нужен номер задачи цифрами. Посмотреть номера можно в /list.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(AddTaskStates.waiting_for_task, F.text)
async def process_task_text(message: types.Message, state: FSMContext) -> None:
    """Принимает текст задачи и переводит диалог к выбору категории.

    В БД ещё не пишем: сначала кладём текст во FSM-данные.
    """
    text = (message.text or "").strip()

    if not text:
        await message.answer("Текст задачи не может быть пустым. Попробуйте ещё раз.")
        return

    await state.update_data(task_text=text)
    await state.set_state(AddTaskStates.waiting_for_category)
    await message.answer(
        "Выберите категорию или напишите свою.\n"
        "Например: Работа, Дом, Учеба.",
        reply_markup=category_keyboard(),
    )


@router.message(AddTaskStates.waiting_for_task)
async def process_task_non_text(message: types.Message) -> None:
    """Реакция на не-текст в состоянии waiting_for_task."""
    await message.answer(
        "Нужен обычный текст задачи, а не файл или стикер. Напишите задачу словами."
    )


@router.message(AddTaskStates.waiting_for_category, F.text)
async def process_task_category(message: types.Message, state: FSMContext) -> None:
    """Принимает категорию, сохраняет задачу и закрывает FSM.

    Любой непустой текст — валидная категория, не только кнопки.
    """
    category = (message.text or "").strip()
    if not category:
        await message.answer("Категория не может быть пустой. Напишите название или нажмите кнопку.")
        return

    data = await state.get_data()
    task_text = data.get("task_text")
    if not task_text:
        # На всякий случай, если FSM-данные потерялись (например, после перезапуска).
        await state.clear()
        await message.answer(
            "Не удалось найти текст задачи. Начните заново через /add.",
            reply_markup=main_menu_keyboard(),
        )
        return

    task_id = await add_task(
        text=task_text,
        user=user_label(message.from_user),
        category=category,
    )
    await state.clear()
    await message.answer(
        f"Задача сохранена. ID: {task_id}\n"
        f"{task_text}\n"
        f"Категория: {category}\n"
        f"Статус: {DEFAULT_STATUS}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(AddTaskStates.waiting_for_category)
async def process_category_non_text(message: types.Message) -> None:
    """Реакция на не-текст, когда бот ждёт категорию."""
    await message.answer(
        "Нужен текст категории. Нажмите кнопку или напишите своё название.",
        reply_markup=category_keyboard(),
    )


@router.message()
async def fallback_welcome(message: types.Message, state: FSMContext) -> None:
    """Любое сообщение вне команд и FSM — то же приветствие, что у /start.

    Так пользователь всегда видит список команд и кнопки меню.
    """
    await state.clear()
    await send_welcome(message)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует команды в системном меню Telegram (кнопка «/» слева от поля ввода)."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="меню/start — приветствие и список команд"),
            BotCommand(command="add", description="Добавить задачу"),
            BotCommand(command="list", description="Показать все задачи"),
            BotCommand(command="list_csv", description="Скачать задачи в CSV"),
            BotCommand(command="done", description="Отметить задачу выполненной"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )


async def main() -> None:
    """Запускает бота: готовит БД, создаёт Bot/Dispatcher и начинает polling.

    MemoryStorage хранит FSM в оперативной памяти — для домашки достаточно.
    После перезапуска незавершённый /add придётся начать заново.
    """
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан токен бота. Создайте файл .env рядом с main.py "
            "(см. .env.example) и укажите BOT_TOKEN=... "
            "либо задайте переменную окружения BOT_TOKEN."
        )

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    await setup_bot_commands(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен. Ожидаю сообщения...")
    # drop_pending_updates=True — не обрабатывать старые апдейты, накопленные офлайн.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    # asyncio.run нужен, потому что aiogram 3 полностью асинхронный.
    asyncio.run(main())
