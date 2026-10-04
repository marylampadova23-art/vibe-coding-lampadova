"""Telegram-бот для сбора обратной связи и работы с каталогом обучений из CSV.

Python 3.9+; python-telegram-bot==21.11.1; python-dotenv==1.1.1.
Запуск: python bot.py (из активированного виртуального окружения).
BOT_TOKEN и ADMIN_ID берутся из .env рядом с этим файлом.
Каталог обучений читается из trainings.csv рядом с bot.py.
"""

import asyncio
import csv
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Все локальные файлы ищем рядом с bot.py, независимо от текущей папки Terminal.
BASE_DIR = Path(__file__).resolve().parent

# Локально база хранится рядом с bot.py.
# В облаке DATA_DIR можно направить на постоянный диск.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "feedback.db"
TRAININGS_PATH = BASE_DIR / "trainings.csv"
TRAINING, RATING, COMMENT = range(3)
MAX_TRAINING = 120
MAX_COMMENT = 1500
PRIVATE = filters.ChatType.PRIVATE
TEXT = PRIVATE & filters.TEXT & ~filters.COMMAND

RATING_KEYBOARD = ReplyKeyboardMarkup(
    [["1", "2", "3", "4", "5"]],
    resize_keyboard=True,
    one_time_keyboard=True,
    input_field_placeholder="Оценка от 1 до 5",
)

LOG = logging.getLogger("feedback_bot")

HELP_TEXT = (
    "Я собираю обратную связь после обучения и умею работать с каталогом обучений.\n\n"
    "Обратная связь:\n"
    "/feedback — оставить оценку и комментарий\n"
    "/cancel — отменить заполнение\n"
    "/skip — пропустить комментарий на последнем шаге\n"
    "/report — отчёт, только для руководителя\n\n"
    "Каталог обучений из CSV:\n"
    "/trainings — показать все обучения\n"
    "/training <название> — найти обучение по названию\n"
    "/department <отдел> — показать обучения подразделения\n\n"
    "Дополнительно:\n"
    "/myid — узнать свой числовой Telegram ID\n"
    "/help — помощь\n\n"
    "Сохранённые отзывы и статистика доступны руководителю. "
    "Бот не записывает имена, username и Telegram ID участников в базу отзывов. "
    "Сообщения при этом проходят через Telegram — это не гарантия полной анонимности.\n\n"
    "Для лабораторной используйте только вымышленные данные. "
    "Не указывайте в комментариях контакты и личные данные."
)

REQUIRED_TRAINING_COLUMNS = ("name", "department", "format", "date", "owner")


class TrainingDataError(Exception):
    """Понятная ошибка чтения CSV, которую можно безопасно показать пользователю."""


class SafeFormatter(logging.Formatter):
    """Скрываем токен даже в случайном сообщении сторонней библиотеки."""

    def __init__(self, token: str) -> None:
        super().__init__("%(asctime)s | %(levelname)s | %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.token:
            text = text.replace(self.token, "[TOKEN HIDDEN]")
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[TOKEN HIDDEN]", text)


def configure_logging(token: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(token))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    LOG.setLevel(logging.INFO)
    # HTTP-логи могут содержать URL с токеном; их не выводим.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)


def read_settings() -> Tuple[str, Optional[int]]:
    load_dotenv(BASE_DIR / ".env", override=False)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("В файле .env не заполнен BOT_TOKEN. Вставьте токен из BotFather.")

    raw_admin = os.getenv("ADMIN_ID", "").strip()
    if not raw_admin:
        return token, None
    if not raw_admin.isascii() or not raw_admin.isdigit() or int(raw_admin) <= 0:
        raise ValueError("ADMIN_ID должен содержать положительное целое число, без @ и пробелов.")
    return token, int(raw_admin)


# -------------------- CSV: каталог обучений --------------------

def load_trainings() -> List[Dict[str, str]]:
    """Читает trainings.csv и валидирует структуру и каждую строку."""
    if not TRAININGS_PATH.exists():
        raise TrainingDataError("Файл trainings.csv не найден рядом с bot.py.")
    if TRAININGS_PATH.stat().st_size == 0:
        raise TrainingDataError("Файл trainings.csv пуст.")

    try:
        with TRAININGS_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                raise TrainingDataError("В trainings.csv нет строки с заголовками столбцов.")

            fieldnames = [name.strip() for name in reader.fieldnames if name is not None]
            missing = [name for name in REQUIRED_TRAINING_COLUMNS if name not in fieldnames]
            if missing:
                raise TrainingDataError(
                    "В trainings.csv отсутствуют обязательные столбцы: {}.".format(
                        ", ".join(missing)
                    )
                )

            trainings: List[Dict[str, str]] = []
            for line_number, raw_row in enumerate(reader, start=2):
                if raw_row is None:
                    raise TrainingDataError("Некорректная строка {} в trainings.csv.".format(line_number))

                # Лишние значения без заголовка попадают в ключ None.
                if None in raw_row:
                    raise TrainingDataError(
                        "В строке {} больше значений, чем столбцов в заголовке.".format(line_number)
                    )

                row = {
                    column: (raw_row.get(column) or "").strip()
                    for column in REQUIRED_TRAINING_COLUMNS
                }
                empty_columns = [column for column, value in row.items() if not value]
                if empty_columns:
                    raise TrainingDataError(
                        "В строке {} не заполнены поля: {}.".format(
                            line_number, ", ".join(empty_columns)
                        )
                    )

                try:
                    datetime.strptime(row["date"], "%Y-%m-%d")
                except ValueError:
                    raise TrainingDataError(
                        "В строке {} дата должна быть в формате YYYY-MM-DD.".format(line_number)
                    )

                trainings.append(row)
    except UnicodeDecodeError:
        raise TrainingDataError("Не удалось прочитать trainings.csv как UTF-8 файл.")
    except OSError:
        raise TrainingDataError("Не удалось открыть trainings.csv. Проверьте доступ к файлу.")

    if not trainings:
        raise TrainingDataError("В trainings.csv нет ни одного обучения.")
    return trainings


def format_training(item: Dict[str, str]) -> str:
    return (
        "Обучение: {name}\n"
        "Подразделение: {department}\n"
        "Формат: {format}\n"
        "Дата: {date}\n"
        "Ответственный: {owner}"
    ).format(**item)


async def trainings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает весь каталог обучений из CSV."""
    try:
        trainings = load_trainings()
    except TrainingDataError as error:
        await update.effective_message.reply_text("Не удалось прочитать каталог обучений: {}".format(error))
        return

    parts = ["Доступные обучения ({}):".format(len(trainings))]
    for index, item in enumerate(trainings, start=1):
        parts.append("\n{}. {}".format(index, format_training(item)))
    await send_long(context.bot, update.effective_chat.id, "\n".join(parts))


async def training_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ищет обучение по части названия без учёта регистра."""
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "После команды укажите часть названия обучения.\nПример: /training адаптация"
        )
        return

    try:
        trainings = load_trainings()
    except TrainingDataError as error:
        await update.effective_message.reply_text("Не удалось прочитать каталог обучений: {}".format(error))
        return

    query_folded = query.casefold()
    found = [item for item in trainings if query_folded in item["name"].casefold()]
    if not found:
        await update.effective_message.reply_text(
            "По запросу «{}» обучение не найдено. Попробуйте /trainings.".format(query)
        )
        return

    text = "Найдено обучений: {}\n\n{}".format(
        len(found), "\n\n".join(format_training(item) for item in found)
    )
    await send_long(context.bot, update.effective_chat.id, text)


async def department_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает обучения выбранного подразделения без учёта регистра."""
    department = " ".join(context.args).strip()
    if not department:
        await update.effective_message.reply_text(
            "После команды укажите подразделение.\nПример: /department HR"
        )
        return

    try:
        trainings = load_trainings()
    except TrainingDataError as error:
        await update.effective_message.reply_text("Не удалось прочитать каталог обучений: {}".format(error))
        return

    department_folded = department.casefold()
    found = [
        item for item in trainings
        if item["department"].casefold() == department_folded
    ]
    if not found:
        await update.effective_message.reply_text(
            "Для подразделения «{}» обучения не найдены. Попробуйте /trainings.".format(department)
        )
        return

    text = "Обучения подразделения «{}»: {}\n\n{}".format(
        department,
        len(found),
        "\n\n".join(format_training(item) for item in found),
    )
    await send_long(context.bot, update.effective_chat.id, text)


# -------------------- SQLite: отзывы --------------------

def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(str(DB_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    # В таблице НЕТ имени, username и Telegram ID участника.
    with closing(connect_db()) as connection, connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                training TEXT NOT NULL CHECK(length(training) BETWEEN 1 AND 120),
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '' CHECK(length(comment) <= 1500),
                created_at TEXT NOT NULL
            )"""
        )


def insert_feedback(training: str, rating: int, comment: str) -> Tuple[int, str]:
    """Параметры SQL передаём отдельно: введённый текст не становится SQL-кодом."""
    if not (1 <= len(training) <= MAX_TRAINING and 1 <= rating <= 5):
        raise ValueError("Некорректное название обучения или оценка.")
    if len(comment) > MAX_COMMENT:
        raise ValueError("Слишком длинный комментарий.")
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with closing(connect_db()) as connection, connection:
        cursor = connection.execute(
            "INSERT INTO feedback (training, rating, comment, created_at) VALUES (?, ?, ?, ?)",
            (training, rating, comment, created_at),
        )
        feedback_id = int(cursor.lastrowid)
    return feedback_id, created_at


def build_report() -> str:
    with closing(connect_db()) as connection:
        total = connection.execute(
            "SELECT COUNT(*) AS n, AVG(rating) AS average FROM feedback"
        ).fetchone()
        if total["n"] == 0:
            return "Пока нет сохранённых отзывов. Первый отзыв можно оставить через /feedback."
        courses = connection.execute(
            "SELECT training, COUNT(*) AS n, AVG(rating) AS average "
            "FROM feedback GROUP BY training ORDER BY training"
        ).fetchall()
        recent = connection.execute(
            "SELECT id, training, rating, comment, created_at "
            "FROM feedback ORDER BY id DESC LIMIT 10"
        ).fetchall()

    lines = [
        "Отчёт по обратной связи",
        "Всего отзывов: {}".format(total["n"]),
        "Средняя оценка: {:.2f} из 5".format(total["average"]),
        "",
        "По каждому обучению:",
    ]
    for item in courses:
        lines.append(
            "• {}: отзывов — {}, средняя — {:.2f}/5".format(
                item["training"], item["n"], item["average"]
            )
        )
    lines.extend(["", "Последние {} отзывов:".format(len(recent))])
    for item in recent:
        lines.extend(
            [
                "",
                "№{} | {}".format(item["id"], item["created_at"]),
                "Обучение: {}".format(item["training"]),
                "Оценка: {}/5".format(item["rating"]),
                "Комментарий: {}".format(item["comment"] or "не оставлен"),
            ]
        )
    return "\n".join(lines)


def split_message(text: str) -> List[str]:
    # Держимся заметно ниже лимита Telegram 4096 символов.
    pieces: List[str] = []
    while len(text) > 1800:
        end = text.rfind("\n", 0, 1801)
        if end < 1:
            end = 1800
        pieces.append(text[:end])
        text = text[end:].lstrip("\n")
    if text:
        pieces.append(text)
    return pieces


async def send_long(bot, chat_id: int, text: str) -> None:
    for index, part in enumerate(split_message(text)):
        if index:
            await asyncio.sleep(0.5)
        try:
            await bot.send_message(chat_id=chat_id, text=part, disable_web_page_preview=True)
        except RetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + 1)
            await bot.send_message(chat_id=chat_id, text=part, disable_web_page_preview=True)


# -------------------- Команды и диалог обратной связи --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text(HELP_TEXT, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Ваш Telegram ID: {}\n\n"
        "Для настройки руководителя укажите это число в ADMIN_ID в файле .env, "
        "перезапустите программу и отправьте /start. "
        "Сам ID не даёт доступ к отчёту без настройки владельцем бота.".format(
            update.effective_user.id
        )
    )


async def begin_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"] = {}
    await update.effective_message.reply_text(
        "Как называлось обучение? Напишите название (до 120 символов).\n"
        "Используйте одинаковое название для отзывов об одном обучении.\n"
        "Отменить заполнение: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TRAINING


async def receive_training(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    training = " ".join(update.effective_message.text.split())
    if not 1 <= len(training) <= MAX_TRAINING:
        await update.effective_message.reply_text("Нужно непустое название длиной до 120 символов.")
        return TRAINING
    context.user_data["draft"] = {"training": training}
    await update.effective_message.reply_text(
        "Как вы оцениваете обучение?\n1 — совсем не понравилось; 5 — очень понравилось.\n"
        "Нажмите кнопку или отправьте цифру от 1 до 5.",
        reply_markup=RATING_KEYBOARD,
    )
    return RATING


async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if text not in {"1", "2", "3", "4", "5"}:
        await update.effective_message.reply_text(
            "Выберите оценку от 1 до 5.", reply_markup=RATING_KEYBOARD
        )
        return RATING

    draft = context.user_data.get("draft")
    if not draft or "training" not in draft:
        await update.effective_message.reply_text("Начните новый отзыв: /feedback.")
        return ConversationHandler.END

    draft["rating"] = int(text)
    await update.effective_message.reply_text(
        "Напишите, что понравилось и что можно улучшить (до 1500 символов).\n"
        "Можно пропустить комментарий: /skip. Отменить весь отзыв: /cancel.\n"
        "Не указывайте личные данные.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return COMMENT


async def finish_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str
) -> int:
    draft = context.user_data.get("draft")
    if not draft or "training" not in draft or "rating" not in draft:
        await update.effective_message.reply_text("Нет активного отзыва. Начните заново: /feedback.")
        return ConversationHandler.END

    try:
        number, created_at = insert_feedback(draft["training"], draft["rating"], comment)
    except sqlite3.Error:
        LOG.error("Не удалось сохранить отзыв в SQLite. Проверьте доступ к папке и свободное место.")
        await update.effective_message.reply_text(
            "Не удалось сохранить отзыв. Повторите комментарий или /skip немного позже.\n"
            "Для отмены: /cancel."
        )
        return COMMENT

    context.user_data.pop("draft", None)
    admin_id = context.bot_data.get("admin_id")
    if admin_id is not None:
        notification = (
            "Новый отзыв №{}\nОбучение: {}\nОценка: {}/5\n"
            "Комментарий: {}\nДата: {}\n\nСводный отчёт: /report"
        ).format(
            number,
            draft["training"],
            draft["rating"],
            comment or "не оставлен",
            created_at,
        )
        try:
            await send_long(context.bot, admin_id, notification)
        except TelegramError as error:
            LOG.warning("Отзыв сохранён, но уведомление не доставлено (%s).", type(error).__name__)
    else:
        LOG.warning("Отзыв сохранён. Уведомления отключены: ADMIN_ID ещё не заполнен.")

    try:
        await update.effective_message.reply_text(
            "Спасибо! Отзыв №{} сохранён.\nОставить ещё один: /feedback.".format(number),
            reply_markup=ReplyKeyboardRemove(),
        )
    except TelegramError:
        LOG.warning("Отзыв сохранён, но подтверждение участнику не доставлено.")
    return ConversationHandler.END


async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment = update.effective_message.text.strip()
    if not comment:
        await update.effective_message.reply_text("Напишите комментарий или отправьте /skip.")
        return COMMENT
    if len(comment) > MAX_COMMENT:
        await update.effective_message.reply_text("Сократите комментарий до 1500 символов или /skip.")
        return COMMENT
    return await finish_feedback(update, context, comment)


async def skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await finish_feedback(update, context, "")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    had_draft = context.user_data.pop("draft", None) is not None
    text = (
        "Заполнение отменено. Незавершённый отзыв не сохранён."
        if had_draft
        else "Нет активного отзыва."
    )
    await update.effective_message.reply_text(
        text + "\nНачать новый: /feedback.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = context.bot_data.get("admin_id")
    if admin_id is None or update.effective_user.id != admin_id:
        await update.effective_message.reply_text("Этот отчёт доступен только руководителю.")
        return
    try:
        text = build_report()
    except sqlite3.Error:
        LOG.error("Не удалось прочитать SQLite для отчёта.")
        await update.effective_message.reply_text("Не удалось сформировать отчёт. Попробуйте позже.")
        return
    await send_long(context.bot, update.effective_chat.id, text)


async def wrong_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Здесь нужен текст или кнопка оценки, не вложение. Для отмены: /cancel."
    )


async def wrong_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Команда недоступна на этом шаге. /skip работает только вместо комментария.\n"
        "Помощь: /help. Новый отзыв: /feedback. Отмена: /cancel."
    )


async def outside_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Чтобы оставить отзыв, отправьте /feedback. Каталог обучений: /trainings. "
        "Список команд: /help.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def private_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Бот работает только в личном чате. Напишите мне напрямую."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        LOG.error("Обнаружен второй запуск бота. Закройте другие копии программы и запустите одну.")
        context.application.stop_running()
        return
    LOG.error("Ошибка обработки: %s. Содержимое сообщения не записывается.", type(error).__name__)
    if (
        isinstance(update, Update)
        and update.effective_message
        and update.effective_chat.type == "private"
    ):
        try:
            await update.effective_message.reply_text(
                "Не удалось завершить действие. Проверьте соединение и попробуйте позже.\n"
                "Если вы завершали отзыв, он мог уже сохраниться — уточните у организатора, "
                "прежде чем отправлять его повторно."
            )
        except TelegramError:
            pass


async def on_startup(application: Application) -> None:
    commands = [
        BotCommand("start", "Начало работы"),
        BotCommand("feedback", "Оставить отзыв"),
        BotCommand("trainings", "Все обучения из CSV"),
        BotCommand("training", "Найти обучение по названию"),
        BotCommand("department", "Обучения подразделения"),
        BotCommand("help", "Помощь"),
        BotCommand("cancel", "Отменить заполнение"),
        BotCommand("skip", "Пропустить комментарий"),
        BotCommand("myid", "Узнать свой Telegram ID"),
        BotCommand("report", "Отчёт для руководителя"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except TelegramError:
        LOG.warning("Меню команд не обновлено. Команды всё равно можно вводить вручную.")
    LOG.info("Бот запущен. Откройте его в Telegram и отправьте /start. Остановка: Control + C.")
    if application.bot_data.get("admin_id") is None:
        LOG.warning("ADMIN_ID пока пуст: получите /myid, заполните .env и перезапустите бота.")


def make_application(token: str, admin_id: Optional[int]) -> Application:
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(False)
        .post_init(on_startup)
        .build()
    )
    app.bot_data["admin_id"] = admin_id

    common_commands = [
        CommandHandler("start", start, filters=PRIVATE),
        CommandHandler("help", help_command, filters=PRIVATE),
        CommandHandler("myid", my_id, filters=PRIVATE),
        CommandHandler("report", report, filters=PRIVATE),
        CommandHandler("trainings", trainings_command, filters=PRIVATE),
        CommandHandler("training", training_command, filters=PRIVATE),
        CommandHandler("department", department_command, filters=PRIVATE),
        CommandHandler("cancel", cancel, filters=PRIVATE),
    ]

    conversation = ConversationHandler(
        entry_points=[CommandHandler("feedback", begin_feedback, filters=PRIVATE)],
        states={
            TRAINING: [MessageHandler(TEXT, receive_training)],
            RATING: [MessageHandler(TEXT, receive_rating)],
            COMMENT: [
                CommandHandler("skip", skip_comment, filters=PRIVATE),
                MessageHandler(TEXT, receive_comment),
            ],
        },
        fallbacks=common_commands
        + [
            MessageHandler(PRIVATE & filters.COMMAND, wrong_command),
            MessageHandler(PRIVATE & ~filters.TEXT, wrong_content),
        ],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )

    app.add_handler(conversation)
    for handler in common_commands:
        app.add_handler(handler)
    app.add_handler(MessageHandler(PRIVATE & filters.COMMAND, wrong_command))
    app.add_handler(MessageHandler(PRIVATE, outside_conversation))
    app.add_handler(MessageHandler(~PRIVATE, private_only))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    try:
        token, admin_id = read_settings()
    except ValueError as error:
        print("Ошибка настройки: {}".format(error))
        return

    configure_logging(token)
    try:
        init_db()
        app = make_application(token, admin_id)
        app.run_polling(allowed_updates=["message"], drop_pending_updates=False)
    except InvalidToken:
        LOG.error("Telegram не принял токен. Проверьте BOT_TOKEN в .env. Не присылайте токен в чат.")
    except NetworkError:
        LOG.error("Не удалось подключиться к Telegram. Проверьте интернет и повторите python bot.py.")
    except sqlite3.Error:
        LOG.error("Не удалось открыть базу данных. Проверьте доступ к папке проекта и свободное место.")
    except TelegramError as error:
        LOG.error("Ошибка Telegram: %s. Значения секретов не выводятся.", type(error).__name__)
    except Exception as error:
        LOG.error("Ошибка запуска: %s. Значения секретов не выводятся.", type(error).__name__)


if __name__ == "__main__":
    main()
