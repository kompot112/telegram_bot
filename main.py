# Взаимодействие с API GPT идет через сервер (так как в России доступ к нему ограничен)
# скрипт с используемыми эндпоинтами прилагается в папке (server.py)
# Все действия пользователей заносятся в БД на том же сервере




import logging
import httpx
from aiogram import Bot, Dispatcher, types          # Используется aiogram==2.25.*
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.middlewares import BaseMiddleware
from httpx import ReadTimeout
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import ssl
import re
import html
import asyncio

ssl._create_default_https_context = ssl._create_unverified_context

# Импортируются модули БД MySQL для изменений на сервере
from config import BOT_TOKEN, FLASK_SERVER_URL
from db import (
    init_db,
    register_user,
    save_real_name,
    get_user_by_chat_id,
    log_user_action,
    get_all_chat_ids
)

#logging.basicConfig(level=logging.DEBUG)  # Подключен дебаг для подробного логирования
logger = logging.getLogger(__name__)

# ------------------ Вспомогательные Функции ------------------

def escape_html_func(text: str) -> str:
    return html.escape(text)

# ----------------- Планировщик ---------------------
async def send_daily_reminders():
    chat_ids = get_all_chat_ids()
    reminder_text = "Доброе утро! Не забудьте пройти сегодня ваши задания."
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, reminder_text, parse_mode="HTML")
            logger.info(f"Отправлено напоминание пользователю {chat_id}")
            await asyncio.sleep(0.1)  # Задержка 100 мс между отправками
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание пользователю {chat_id}: {e}")

def setup_scheduler():
    scheduler = AsyncIOScheduler(timezone="UTC")  
    scheduler.add_job(send_daily_reminders, 'cron', hour=7, minute=0)  # Каждый день в 10:00 UTC+3 (для Москвы)
    scheduler.start()
    logger.info("Планировщик напоминаний запущен в UTC.")

# ------------------ LangChain-массив ------------------
langchain_context = []

def add_context(role: str, content: str):
    """
    Добавляем сообщение в общий контекст
    (role: 'user' или 'assistant')
    """
    langchain_context.append({"role": role, "content": content})
    #logger.debug(f"Added to context: {role} - {content}")

def get_context():
    """Возвращаем весь контекст"""
    return langchain_context

# ------------------ Инициализация бота ------------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# Классы состояний
class RegistrationStates(StatesGroup):
    waiting_for_real_first_name = State()
    waiting_for_real_last_name = State()

class OpenQuestionStates(StatesGroup):
    waiting_for_topic = State()
    waiting_for_complexity = State()
    waiting_for_answers = State()

class TestStates(StatesGroup):
    waiting_for_test_topic = State()
    waiting_for_complexity = State()
    answering = State()

# ------------------ Мидлвари ------------------
class AuthMiddleware(BaseMiddleware):
    async def on_pre_process_update(self, update: types.Update, data: dict):
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query:
            chat_id = update.callback_query.message.chat.id

        if chat_id is not None:
            user = get_user_by_chat_id(chat_id)
            data['user'] = user
            #logger.debug(f"AuthMiddleware: Проверка пользователя для chat_id={chat_id}")

            if not user:
                if update.message and update.message.text != "/start":
                    await update.message.answer(
                        escape_html_func("Пожалуйста, зарегистрируйтесь командой `/start`."),
                        parse_mode="HTML"
                    )
                    logger.warning(f"AuthMiddleware: Пользователь chat_id={chat_id} не зарегистрирован.")
                    return

                elif update.callback_query and update.callback_query.data != "/start":
                    await update.callback_query.message.answer(
                        escape_html_func("Пожалуйста, зарегистрируйтесь командой `/start`."),
                        parse_mode="HTML"
                    )
                    logger.warning(f"AuthMiddleware: Пользователь chat_id={chat_id} не зарегистрирован.")
                    await update.callback_query.answer()
                    return

class LoggingMiddleware(BaseMiddleware):
    async def on_post_process_update(self, update: types.Update, results, data: dict):
        user = data.get("user")
        if not user:
            return

        user_id = user["id"]
        if update.message:
            text = update.message.text or ""
            log_user_action(user_id, f"User message: {text}")
            #logger.info(f"LoggingMiddleware: Пользователь {user_id} отправил сообщение: {text}")
        elif update.callback_query:
            log_user_action(user_id, f"Callback: {update.callback_query.data}")
            #logger.info(f"LoggingMiddleware: Пользователь {user_id} нажал кнопку: {update.callback_query.data}")

dp.middleware.setup(AuthMiddleware())
dp.middleware.setup(LoggingMiddleware())

# ------------------ Планировщик ------------------
scheduler = AsyncIOScheduler(timezone="UTC")  

async def on_startup(dp):
    logging.info("Запуск on_startup: инициализация планировщика...")

    await bot.set_my_commands([
        types.BotCommand("start", "Регистрация"),
        types.BotCommand("menu", "Показать меню"),
        types.BotCommand("generate_questions", "Выбрать тип вопросов (тест / открытые)"),
    ])

    setup_scheduler()
    logging.info("Планировщик успешно запущен!")

# ------------------ Базовые команды ------------------
@dp.message_handler(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    try:
        register_user(
            chat_id=message.chat.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        logger.info(f"Пользователь {message.chat.id} зарегистрирован.")

        user = get_user_by_chat_id(message.chat.id)

        if not user["real_first_name"] or not user["real_last_name"]:
            await message.answer(
                escape_html_func("Здравствуйте! Введите ваше настоящее имя:"),
                parse_mode="HTML"
            )
            await RegistrationStates.waiting_for_real_first_name.set()
        else:
            success_message = escape_html_func("Вы уже зарегистрированы. Можете использовать `/menu` для просмотра функций.")
            await message.answer(success_message, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}")
        error_message = escape_html_func("Произошла ошибка при регистрации. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

@dp.message_handler(state=RegistrationStates.waiting_for_real_first_name, content_types=types.ContentTypes.TEXT)
async def process_real_first_name(message: types.Message, state: FSMContext):
    await state.update_data(real_first_name=message.text)
    #logger.debug(f"Пользователь {message.chat.id} ввёл имя: {message.text}")
    await message.answer(
        escape_html_func("Теперь введите вашу настоящую фамилию:"),
        parse_mode="HTML"
    )
    await RegistrationStates.waiting_for_real_last_name.set()

@dp.message_handler(state=RegistrationStates.waiting_for_real_last_name, content_types=types.ContentTypes.TEXT)
async def process_real_last_name(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        real_first_name = data.get("real_first_name")
        real_last_name = message.text

        save_real_name(message.chat.id, real_first_name, real_last_name)
        logger.info(f"Пользователь {message.chat.id} завершил регистрацию: {real_first_name} {real_last_name}")

        success_message = escape_html_func(
            "Регистрация завершена!\nИспользуйте `/generate_questions` для выбора типа вопросов."
        )
        await message.answer(success_message, parse_mode="HTML")
        await state.finish()


    except Exception as e:
        logger.error(f"Ошибка в process_real_last_name: {e}")
        error_message = escape_html_func("Произошла ошибка при сохранении данных. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

@dp.message_handler(Command("menu"))
async def cmd_menu(message: types.Message):
    try:
        menu_message = (
            "Команды:\n"
            "<code>/start</code> - Регистрация\n"
            "<code>/generate_questions</code> - Создать тестовые/открытые вопросы\n"
            "<code>/menu</code> - Показать это меню\n\n\n"
            "<b>ПРИМЕЧАНИЕ</b>: нельзя просто вывести эти команды в виде текста, чтобы при нажатии на них они автоматически отправились в бота, так как используется parse mode."
            " Это связано с экранированием специальных символов. Поэтому команды размещены в отдельной клавиатуре для удобства."
        )
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        keyboard.add("/start", "/generate_questions", "/menu")

        await message.answer(menu_message, reply_markup=keyboard, parse_mode="HTML")
        logger.info(f"Пользователь {message.chat.id} вызвал /menu.")
    except Exception as e:
        logger.error(f"Ошибка в cmd_menu: {e}")
        error_message = "Произошла ошибка при обработке команды /menu."
        await message.answer(error_message, parse_mode=None)

# ------------------ Генерация вопросов ------------------
@dp.message_handler(Command("generate_questions"))
async def cmd_generate_questions(message: types.Message):
    try:
        keyboard = types.InlineKeyboardMarkup()
        btn_test = types.InlineKeyboardButton(
            text="Тестовые вопросы",
            callback_data="choose_question_type_test"
        )
        btn_open = types.InlineKeyboardButton(
            text="Открытые вопросы",
            callback_data="choose_question_type_open"
        )
        keyboard.add(btn_test, btn_open)

        prompt_text = escape_html_func("Выберите, какие вопросы вы хотите сгенерировать:")
        #logger.debug(f"Prompt to send: {prompt_text}")
        await message.answer(prompt_text, reply_markup=keyboard, parse_mode="HTML")
        logger.info(f"Пользователь {message.chat.id} выбрал генерацию вопросов.")
    except Exception as e:
        logger.error(f"Ошибка в cmd_generate_questions: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке команды. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith("choose_question_type_"))
async def process_question_type_callback(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        choice = callback_query.data.replace("choose_question_type_", "")
        #logger.debug(f"Пользователь выбрал тип вопросов: {choice}")
        if choice == "test":
            prompt = escape_html_func("Введите тему, по которой хотите получить ТЕСТОВЫЕ вопросы:")
            await callback_query.message.answer(prompt, parse_mode="HTML")
            await TestStates.waiting_for_test_topic.set()

        elif choice == "open":
            prompt = escape_html_func("Введите тему, по которой хотите получить ОТКРЫТЫЕ вопросы:")
            await callback_query.message.answer(prompt, parse_mode="HTML")
            await OpenQuestionStates.waiting_for_topic.set()

        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка в process_question_type_callback: {e}")
        error_message = escape_html_func("Произошла ошибка при выборе типа вопросов. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await callback_query.answer()

# =========== ОТКРЫТЫЕ ВОПРОСЫ ===========
@dp.message_handler(state=OpenQuestionStates.waiting_for_topic, content_types=types.ContentTypes.TEXT)
async def open_questions_topic(message: types.Message, state: FSMContext):
    try:
        topic = message.text.strip()
        await state.update_data(topic=topic)
        logger.info(f"Пользователь {message.chat.id} выбрал тему для открытых вопросов: {topic}")
        # Спрашиваем сложность
        keyboard = types.InlineKeyboardMarkup(row_width=5)
        buttons = [
            types.InlineKeyboardButton(str(i), callback_data=f"choose_open_complexity_{i}")
            for i in range(1, 11)
        ]
        keyboard.add(*buttons)

        prompt = escape_html_func("Выберите сложность вопросов (1 – легко, 10 – сложно):")
        #logger.debug(f"Prompt to send: {prompt}")
        await message.answer(prompt, reply_markup=keyboard, parse_mode="HTML")
        await OpenQuestionStates.waiting_for_complexity.set()
    except Exception as e:
        logger.error(f"Ошибка в open_questions_topic: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке темы. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith("choose_open_complexity_"), state=OpenQuestionStates.waiting_for_complexity)
async def open_questions_complexity_callback(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        complexity_str = callback_query.data.replace("choose_open_complexity_", "")
        complexity = int(complexity_str)
        await state.update_data(complexity=complexity)
        await callback_query.answer(f"Сложность выбрана: {complexity}")
        logger.info(f"Пользователь {callback_query.from_user.id} выбрал сложность {complexity} для открытых вопросов.")

        data = await state.get_data()
        topic = data.get("topic")

        prompt = (
            f"Сгенерируй 5 коротких вопросов по теме: {topic}. "
            f"Уровень сложности: {complexity} (1–10), где 1 - очень просто, 10 - сложность для гениев. "
            "Оформи вопросы по одному на строке."
        )
        # Сохраняем в контекст
        add_context("user", prompt)

        # Генерируем вопросы
        questions = await generate_questions_gpt(prompt)
        if questions and all(isinstance(q, str) for q in questions):
            add_context("assistant", "\n".join(questions))
        else:
            add_context("assistant", "Не удалось сгенерировать вопросы.")

        await state.update_data(questions=questions)

        escaped_questions = "\n".join([escape_html_func(q) for q in questions])
        text_for_user = "<b>Ваши вопросы:</b>\n" + escaped_questions

        static_message = "Теперь отправьте ответы на все 5 вопросов одним сообщением (каждый ответ с новой строки)."
        static_message_escaped = escape_html_func(static_message)
        text_for_user += f"\n{static_message_escaped}"

        # Добавляем кнопку Регенерировать
        keyboard = types.InlineKeyboardMarkup()
        regenerate_btn = types.InlineKeyboardButton(
            text="Регенерировать",
            callback_data="regenerate_open_questions"
        )
        keyboard.add(regenerate_btn)

        #logger.debug(f"Text for user: {text_for_user}")

        await callback_query.message.answer(text_for_user, reply_markup=keyboard, parse_mode="HTML")
        await OpenQuestionStates.waiting_for_answers.set()
    except ValueError:
        await callback_query.answer("Некорректная сложность!")
        logger.warning(f"Пользователь {callback_query.from_user.id} выбрал некорректную сложность.")
    except Exception as e:
        logger.error(f"Ошибка в open_questions_complexity_callback: {e}")
        error_message = escape_html_func("Произошла ошибка при выборе сложности. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "regenerate_open_questions", state=OpenQuestionStates.waiting_for_answers)
async def regenerate_open_questions_callback(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        topic = data.get("topic")
        complexity = data.get("complexity", 5)

        await callback_query.answer("Перегенерируем вопросы...")
        #logger.info(f"Пользователь {callback_query.from_user.id} запросил регенерацию открытых вопросов.")

        prompt = (
            f"Сгенерируй 5 коротких вопросов по теме: {topic}. "
            f"Уровень сложности: {complexity} (1–10). "
            "Оформи вопросы по одному на строке."
        )
        add_context("user", prompt)

        questions = await generate_questions_gpt(prompt)
        if questions and all(isinstance(q, str) for q in questions):
            add_context("assistant", "\n".join(questions))
        else:
            add_context("assistant", "Не удалось сгенерировать вопросы.")

        await state.update_data(questions=questions)

        escaped_questions = "\n".join([escape_html_func(q) for q in questions])
        text_for_user = "<b>Новые вопросы:</b>\n" + escaped_questions

        static_message = "Теперь отправьте ответы на все 5 вопросов одним сообщением (каждый ответ с новой строки)."
        static_message_escaped = escape_html_func(static_message)
        text_for_user += f"\n{static_message_escaped}"

        # Добавляем кнопку Регенерировать
        keyboard = types.InlineKeyboardMarkup()
        regenerate_btn = types.InlineKeyboardButton(
            text="Регенерировать",
            callback_data="regenerate_open_questions"
        )
        keyboard.add(regenerate_btn)

        #logger.debug(f"Text for user: {text_for_user}")

        await callback_query.message.edit_text(text_for_user, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в regenerate_open_questions_callback: {e}")
        error_message = escape_html_func("Произошла ошибка при регенерации вопросов. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await callback_query.answer()

@dp.message_handler(state=OpenQuestionStates.waiting_for_answers, content_types=types.ContentTypes.TEXT)
async def process_open_answers(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        answers = message.text.strip().split("\n")

        if len(answers) < 5:
            prompt = escape_html_func("Пожалуйста, ответьте на все 5 вопросов (каждый ответ с новой строки).")
            await message.answer(prompt, parse_mode="HTML")
            return

        prompt = "Вопросы:\n"
        for i, q in enumerate(questions, start=1):
            prompt += f"{i}. {q}\n"
        prompt += "\nОтветы:\n"
        for i, ans in enumerate(answers, start=1):
            prompt += f"{i}. {ans}\n"
        prompt += "\nОцени правильность каждого ответа и дай краткое пояснение, если ответ неверен."

        add_context("user", f"Пользователь отвечает:\n{prompt}")

        explanation = await check_answers_gpt(prompt)
        explanation_clean = explanation.strip()

        explanation_escaped = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', escape_html_func(explanation_clean)) 
        #logger.debug(f"Экраннированный ответ: {explanation_escaped}")

        add_context("assistant", explanation_escaped)
        await message.answer(explanation_escaped, parse_mode="HTML")
        await state.finish()
        logger.info(f"Пользователь {message.chat.id} завершил ответы на открытые вопросы.")

        # Предложение пройти ещё раз
        suggestion_text = escape_html_func("Не хотите ли пройти ещё раз?")
        keyboard = types.InlineKeyboardMarkup()
        btn_yes = types.InlineKeyboardButton("Да", callback_data="start_again_yes")
        btn_no = types.InlineKeyboardButton("Нет", callback_data="start_again_no")
        keyboard.add(btn_yes, btn_no)
        await message.answer(suggestion_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в process_open_answers: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке ваших ответов. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

# =========== ТЕСТОВЫЕ ВОПРОСЫ ===========
@dp.message_handler(state=TestStates.waiting_for_test_topic, content_types=types.ContentTypes.TEXT)
async def test_questions_topic(message: types.Message, state: FSMContext):
    try:
        topic = message.text.strip()
        await state.update_data(topic=topic)
        #logger.info(f"Пользователь {message.chat.id} выбрал тему для тестовых вопросов: {topic}")

        # Спрашиваем сложность
        keyboard = types.InlineKeyboardMarkup(row_width=5)
        buttons = [
            types.InlineKeyboardButton(str(i), callback_data=f"choose_test_complexity_{i}")
            for i in range(1, 11)
        ]
        keyboard.add(*buttons)

        prompt = escape_html_func("Выберите сложность тестовых вопросов (1 – легко, 10 – сложно):")
        #logger.debug(f"Prompt to send: {prompt}")
        await message.answer(prompt, reply_markup=keyboard, parse_mode="HTML")
        await TestStates.waiting_for_complexity.set()
    except Exception as e:
        logger.error(f"Ошибка в test_questions_topic: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке темы. Пожалуйста, попробуйте позже.")
        await message.answer(error_message, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith("choose_test_complexity_"), state=TestStates.waiting_for_complexity)
async def test_questions_complexity_callback(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        complexity_str = callback_query.data.replace("choose_test_complexity_", "")
        complexity = int(complexity_str)
        await state.update_data(complexity=complexity)
        await callback_query.answer(f"Сложность выбрана: {complexity}")
       # logger.info(f"Пользователь {callback_query.from_user.id} выбрал сложность {complexity} для тестовых вопросов.")

        data = await state.get_data()
        topic = data.get("topic")

        prompt = (
            f"Сгенерируй 5 тестовых вопросов по теме '{topic}' "
            f"с 4 вариантами ответов (A, B, C, D) и укажи правильный ответ. "
            f"Сложность: {complexity} (1–10), где 1 - очень просто, 10 - сложность для гениальных людей."
        )
        add_context("user", prompt)

        test_questions = await generate_test_questions_gpt(topic, complexity)
        if test_questions and isinstance(test_questions, list) and all(isinstance(q, dict) for q in test_questions):
            formatted_questions = "\n".join([
                f"<b>Вопрос {i}:</b> {escape_html_func(q['question'])}<br>"
                f"<b>Ответ:</b> {escape_html_func(q['answer'])}"
                for i, q in enumerate(test_questions, start=1)
            ])
            add_context("assistant", formatted_questions)
        else:
            add_context("assistant", "Не удалось сгенерировать тестовые вопросы.")

        if not test_questions or not isinstance(test_questions, list) or not all(isinstance(q, dict) for q in test_questions):
            error_message = escape_html_func("Извините, не удалось сгенерировать тестовые вопросы.")
            await callback_query.message.answer(error_message, parse_mode="HTML")
            await state.finish()
            return

        await state.update_data(
            test_questions=test_questions,
            current_question_index=0,
            correct_count=0,
            answers=[]  
        )

        await ask_test_question(callback_query.message.chat.id, state)
        await TestStates.answering.set()
    except ValueError:
        await callback_query.answer("Некорректная сложность!")
        logger.warning(f"Пользователь {callback_query.from_user.id} выбрал некорректную сложность.")
    except Exception as e:
        logger.error(f"Ошибка в test_questions_complexity_callback: {e}")
        error_message = escape_html_func("Произошла ошибка при выборе сложности. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await callback_query.answer()

async def ask_test_question(chat_id: int, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("test_questions", [])
        current_index = data.get("current_question_index", 0)

        if current_index >= len(questions):
            correct_count = data.get("correct_count", 0)
            total = len(questions)

            # Получаем все ответы пользователя
            answers = data.get("answers", [])

            report = "<b>Результаты теста:</b>\n\n"
            for answer in answers:
                status = "правильно" if answer["is_correct"] else "неправильно"
                report += f"Вопрос {answer['question_number']} - {status}:\n"
                report += f"Ваш ответ: {answer['user_answer_text']}\n"
                report += f"Верный ответ: {answer['correct_answer_text']}\n\n"

            final_message = f"<b>Тест завершён!</b> Правильных ответов: {correct_count} из {total}.\n\n{report}"
            #logger.debug(f"Final message to send: {final_message}")
            await bot.send_message(chat_id, final_message, parse_mode="HTML")
            #logger.info(f"Пользователь {chat_id} завершил тест: {correct_count}/{total} верно.")
            await state.finish()

            suggestion_text = escape_html_func("Не хотите ли пройти ещё раз?")
            keyboard = types.InlineKeyboardMarkup()
            btn_yes = types.InlineKeyboardButton("Да", callback_data="start_again_yes")
            btn_no = types.InlineKeyboardButton("Нет", callback_data="start_again_no")
            keyboard.add(btn_yes, btn_no)
            await bot.send_message(chat_id, suggestion_text, reply_markup=keyboard, parse_mode="HTML")
            return

        current_q = questions[current_index]
        question_text = escape_html_func(current_q["question"])
        options = current_q["options"]  # ["A) ...", "B) ...", ...]

        # Клавиатура с вариантами
        keyboard = types.InlineKeyboardMarkup()
        for opt in options:
            option_label = opt.split(")")[0]
            callback_data = f"test_answer_{current_index}_{option_label}"
            opt_escaped = escape_html_func(opt)
            keyboard.add(types.InlineKeyboardButton(text=opt_escaped, callback_data=callback_data))

        question_message = f"<b>Вопрос {current_index+1}:</b>\n{question_text}"
        #logger.debug(f"Question message to send: {question_message}")
        await bot.send_message(
            chat_id,
            question_message,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        #logger.debug(f"Пользователь {chat_id} получил вопрос {current_index+1}: {current_q['question']}")
    except Exception as e:
        logger.error(f"Ошибка в ask_test_question: {e}")
        error_message = escape_html_func("Произошла ошибка при отправке вопроса. Пожалуйста, попробуйте позже.")
        await bot.send_message(chat_id, error_message, parse_mode="HTML")
        await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith("test_answer_"), state=TestStates.answering)
async def process_test_answer_callback(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("test_questions", [])
        current_index = data.get("current_question_index", 0)
        correct_count = data.get("correct_count", 0)
        answers = data.get("answers", [])

        parts = callback_query.data.rsplit("_", 2)
        if len(parts) != 3:
            await callback_query.answer("Некорректный формат ответа!")
            logger.warning(f"Пользователь {callback_query.from_user.id} прислал некорректный callback_data: {callback_query.data}")
            return

        _, q_idx_str, chosen_option = parts

        try:
            q_idx = int(q_idx_str)
        except ValueError:
            await callback_query.answer("Некорректный индекс вопроса!")
            logger.warning(f"Пользователь {callback_query.from_user.id} прислал некорректный индекс вопроса: {q_idx_str}")
            return

        if q_idx != current_index:
            await callback_query.answer("Это неактуальный вопрос.")
            logger.warning(f"Пользователь {callback_query.from_user.id} ответил на неактуальный вопрос: {q_idx} != {current_index}")
            return

        current_q = questions[current_index]
        correct_answer_label = current_q["answer"]  # "A" / "B" / "C" / "D"

        # Находим текст правильного ответа
        correct_answer_text = ""
        for opt in current_q["options"]:
            if opt.startswith(correct_answer_label + ")"):
                correct_answer_text = opt.split(")", 1)[1].strip()
                break

        # Находим текст ответа пользователя
        user_answer_text = ""
        for opt in current_q["options"]:
            if opt.startswith(chosen_option.upper() + ")"):
                user_answer_text = opt.split(")", 1)[1].strip()
                break

        if chosen_option.upper() == correct_answer_label.upper():
            correct_count += 1
            is_correct = True
            await callback_query.answer("Верно!")
            #logger.info(f"Пользователь {callback_query.from_user.id} ответил правильно на вопрос {current_index+1}.")
        else:
            is_correct = False
            await callback_query.answer(f"Неверно. Правильный ответ: {correct_answer_label}")
            #logger.info(f"Пользователь {callback_query.from_user.id} ответил неверно на вопрос {current_index+1}: выбрал {chosen_option}, правильно {correct_answer_label}.")

        # Добавляем ответ в список
        answers.append({
            "question_number": current_index + 1,
            "is_correct": is_correct,
            "user_answer_label": chosen_option.upper(),
            "user_answer_text": user_answer_text,
            "correct_answer_label": correct_answer_label.upper(),
            "correct_answer_text": correct_answer_text
        })

        # Обновляем данные состояния
        await state.update_data(
            current_question_index=current_index + 1,
            correct_count=correct_count,
            answers=answers
        )

        # Удаляем клавиатуру у предыдущего вопроса
        await callback_query.message.edit_reply_markup(reply_markup=None)

        # Следующий вопрос или завершение теста
        await ask_test_question(callback_query.message.chat.id, state)
    except Exception as e:
        logger.error(f"Ошибка в process_test_answer_callback: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке ответа. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await state.finish()

# ------------------ Обработка предложения пройти ещё раз ------------------
@dp.callback_query_handler(lambda c: c.data.startswith("start_again_"))
async def handle_start_again(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        choice = callback_query.data.replace("start_again_", "")
       # logger.debug(f"handle_start_again: choice={choice}")

        if choice == "yes":
            # Предлагаем выбрать тип вопросов снова
            keyboard = types.InlineKeyboardMarkup()
            btn_test = types.InlineKeyboardButton(text="Тестовые вопросы", callback_data="choose_question_type_test")
            btn_open = types.InlineKeyboardButton(text="Открытые вопросы", callback_data="choose_question_type_open")
            keyboard.add(btn_test, btn_open)

            prompt_text = escape_html_func("Выберите, какие вопросы вы хотите сгенерировать:")
            await callback_query.message.answer(prompt_text, reply_markup=keyboard, parse_mode="HTML")
            await state.finish()
            #logger.info(f"Пользователь {callback_query.from_user.id} решил пройти ещё раз.")

        elif choice == "no":
            await callback_query.message.answer("Хорошо! Если захотите, используйте `/menu` для выбора команды.", parse_mode="HTML")
            #logger.info(f"Пользователь {callback_query.from_user.id} отказался пройти ещё раз.")

        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка в handle_start_again: {e}")
        error_message = escape_html_func("Произошла ошибка при обработке вашего выбора. Пожалуйста, попробуйте позже.")
        await callback_query.message.answer(error_message, parse_mode="HTML")
        await callback_query.answer()

# ------------------ Обработка неизвестных сообщений ------------------
@dp.message_handler(content_types=types.ContentTypes.ANY, state=None)
async def handle_unknown_message(message: types.Message):
    prompt = escape_html_func("Пожалуйста, выберите команду из меню или используйте `/menu` для отображения доступных команд.")
    await message.reply(prompt, parse_mode="HTML")

# ------------------ Функции взаимодействия с Flask-сервером ------------------
async def generate_questions_gpt(prompt: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{FLASK_SERVER_URL}/generate_questions",
                json={"prompt": prompt}
            )
            response.raise_for_status()
            data = response.json()
            questions = data.get("response", "").split("\n")
            #logger.debug(f"Сгенерированные вопросы: {questions}")
            return questions
    except ReadTimeout:
        logger.error("Ошибка: сервер не ответил вовремя (ReadTimeout).")
        return ["Ошибка: сервер не ответил вовремя (ReadTimeout)."]
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при обращении к серверу: {e}")
        return [f"Ошибка при обращении к серверу: {e}"]

async def generate_test_questions_gpt(topic: str, complexity: int):
    payload = {
        "prompt": (
            f"Сгенерируй 5 тестовых вопросов по теме '{topic}' "
            f"с 4 вариантами ответов (A, B, C, D) и укажи правильный ответ. "
            f"Сложность: {complexity} (1–10)."
        )
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{FLASK_SERVER_URL}/generate_test_questions",
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            questions = data.get("questions", [])
            #logger.debug(f"Сгенерированные тестовые вопросы: {questions}")
            return questions
    except ReadTimeout:
        logger.error("Ошибка: сервер не ответил вовремя (ReadTimeout).")
        return []
    except httpx.HTTPError as e:
        logger.warning(f"Ошибка при обращении к серверу: {e}")
        return []

async def check_answers_gpt(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{FLASK_SERVER_URL}/check_answers",
                json={"prompt": prompt}
            )
            response.raise_for_status()
            data = response.json()
            explanation = data.get("response", "")
            #logger.debug(f"Результаты проверки: {explanation}")
            return explanation
    except ReadTimeout:
        logger.error("Ошибка: сервер не ответил вовремя (ReadTimeout).")
        return "Ошибка: сервер не ответил вовремя (ReadTimeout)."
    except httpx.HTTPError as e:
        logger.error(f"Ошибка при обращении к серверу: {e}")
        return f"Ошибка при обращении к серверу: {e}"

# ------------------ Запуск Бота ------------------
def main():
    init_db()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)

if __name__ == "__main__":
    main()
