import asyncio
import logging
import os
from dotenv import load_dotenv
import aiosqlite  # Импортируем асинхронную базу данных
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN") 
# ID закрытой ГРУППЫ курьеров (должен начинаться с минуса в .env)
COURIERS_CHAT_ID = int(os.getenv("CHAT_ID")) 

# 🛠 Твой номер телефона для Kaspi переводов (измени на свой!)
MY_KASPI_PHONE = "+7 (707) XXX-XX-XX" 

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Автоматически определяем папку, где лежит main.py, чтобы БД создавалась там же
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "BD.db")

# Функция инициализации базы данных (создаем таблицу с полем оплаты и курьера)
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                pvz_address TEXT,
                delivery_address TEXT,
                phone TEXT,
                qr_photo_id TEXT,
                status TEXT DEFAULT 'Новый',
                courier_id INTEGER DEFAULT NULL,
                courier_username TEXT DEFAULT NULL,
                payment_method TEXT DEFAULT 'Не указан'
            )
        ''')
        await db.commit()

# Состояния FSM (добавили шаг выбора оплаты)
class OrderForm(StatesGroup):
    waiting_for_pvz = State()
    waiting_for_qr = State()
    waiting_for_address = State()
    waiting_for_phone = State()
    waiting_for_payment = State()

# Главное меню клиента
main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📦 Заказать доставку (1000 тг)")]],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type in ["group", "supergroup"]:
        return
    await message.answer(
        f"Привет, {message.from_user.first_name}! Я бот доставки посылок в Костанае.\n"
        "Заберем ваш заказ из Temu, WB, Ozon и привезем прямо к двери.",
        reply_markup=main_kb
    )

@dp.message(F.text == "📦 Заказать доставку (1000 тг)")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.waiting_for_pvz)
    await message.answer("Шаг 1 из 5: Напишите адрес пункта выдачи (ПВЗ), откуда нужно забрать посылку:")

@dp.message(OrderForm.waiting_for_pvz)
async def process_pvz(message: Message, state: FSMContext):
    await state.update_data(pvz=message.text)
    await state.set_state(OrderForm.waiting_for_qr)
    await message.answer("Шаг 2 из 5: Отправьте скриншот штрих-кода или QR-кода получения (картинкой):")

@dp.message(OrderForm.waiting_for_qr, F.photo)
async def process_qr(message: Message, state: FSMContext):
    await state.update_data(qr_photo_id=message.photo[-1].file_id)
    await state.set_state(OrderForm.waiting_for_address)
    await message.answer("Шаг 3 из 5: Напишите адрес, куда привезти посылку:")

@dp.message(OrderForm.waiting_for_qr)
async def process_qr_invalid(message: Message):
    await message.answer("Пожалуйста, отправьте именно скриншот (фотографию) кода.")

@dp.message(OrderForm.waiting_for_address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await state.set_state(OrderForm.waiting_for_phone)
    
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить свой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Шаг 4 из 5: Оставьте ваш номер телефона для связи:", reply_markup=phone_kb)

@dp.message(OrderForm.waiting_for_phone, F.text | F.contact)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    
    # Переходим к выбору способа оплаты
    await state.set_state(OrderForm.waiting_for_payment)
    
    payment_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Kaspi перевод"), KeyboardButton(text="💵 Наличные")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Шаг 5 из 5: Выберите удобный способ оплаты:", reply_markup=payment_kb)

@dp.message(OrderForm.waiting_for_payment, F.text.in_({"💳 Kaspi перевод", "💵 Наличные"}))
async def process_payment(message: Message, state: FSMContext):
    payment_method = message.text
    await state.update_data(payment_method=payment_method)
    
    user_data = await state.get_data()
    await state.clear()
    
    # 💾 ЗАПИСЬ В БАЗУ ДАННЫХ
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            '''INSERT INTO orders (user_id, username, pvz_address, delivery_address, phone, qr_photo_id, payment_method) 
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (
                message.from_user.id,
                message.from_user.username or 'нет юзернейма',
                user_data['pvz'],
                user_data['address'],
                user_data['phone'],
                user_data['qr_photo_id'],
                payment_method
            )
        )
        await db.commit()
        order_id = cursor.lastrowid

    await message.answer(
        "🎉 Спасибо! Ваш заказ принят.\nКурьер уже подбирает вашу посылку. Пожалуйста, ожидайте.",
        reply_markup=main_kb
    )
    
    # Генерируем инлайн-кнопку для чата курьеров
    start_inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚴 Взять заказ", callback_data=f"status_take_{order_id}")]
    ])
    
    admin_text = (
        f"🚨 **НОВЫЙ ЗАКАЗ #{order_id}**\n\n"
        f"📍 Откуда (ПВЗ): {user_data['pvz']}\n"
        f"🏠 Куда везти: {user_data['address']}\n"
        f"📞 Телефон: {user_data['phone']}\n"
        f"💰 Оплата: *{payment_method}*\n"
        f"📊 Статус: 🆕 Новый\n\n"
        f"Кто первый нажмет кнопку ниже — забирает заказ!"
    )
    
    # Отправляем карточку в закрытый чат курьеров
    await bot.send_photo(
        chat_id=COURIERS_CHAT_ID, 
        photo=user_data['qr_photo_id'], 
        caption=admin_text, 
        parse_mode="Markdown",
        reply_markup=start_inline_kb
    )

# Хэндлер нажатия кнопки "Взять заказ" курьером
@dp.callback_query(F.data.startswith("status_take_"))
async def handle_take_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    courier_id = callback.from_user.id
    courier_username = callback.from_user.username or callback.from_user.first_name
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Проверяем текущий статус заказа, чтобы избежать одновременного нажатия
        async with db.execute("SELECT status, user_id, pvz_address, delivery_address, phone, payment_method FROM orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.answer("Заказ не найден!", show_alert=True)
                return
            
            status, user_id, pvz, address, phone, payment_method = row
            if status != 'Новый':
                await callback.answer("Этот заказ уже забрал другой курьер!", show_alert=True)
                return
                
            # Закрепляем заказ за нажавшим курьером
            await db.execute(
                "UPDATE orders SET status = 'В работе', courier_id = ?, courier_username = ? WHERE id = ?", 
                (courier_id, courier_username, order_id)
            )
            await db.commit()
            
            # Отправляем клиенту индивидуальный текст в зависимости от типа оплаты
            try:
                if payment_method == "💳 Kaspi перевод":
                    client_text = (
                        f"🚴 Курьер @{courier_username} принял ваш заказ и уже направляется в ПВЗ!\n\n"
                        f"💳 Пожалуйста, оплатите доставку (1000 тг) на Kaspi:\n"
                        f"📞 Номер: `{MY_KASPI_PHONE}`\n"
                        f"После перевода курьер сразу отдаст вам посылку."
                    )
                else:
                    client_text = (
                        f"🚴 Курьер @{courier_username} принял ваш заказ и уже направляется в ПВЗ!\n\n"
                        f"💵 Вы выбрали оплату наличными. Пожалуйста, приготовьте ровно 1000 тг.\n"
                        f"Передайте деньги курьеру прямо перед получением посылки."
                    )
                await bot.send_message(chat_id=user_id, text=client_text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Не удалось отправить уведомление пользователю: {e}")
            
            # Меняем кнопку в чате курьеров на "Доставлено"
            done_inline_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Доставлено (Завершить)", callback_data=f"status_done_{order_id}")]
            ])
            
            updated_text = (
                f"🚴 **ЗАКАЗ #{order_id} В РАБОТЕ**\n\n"
                f"🏃‍♂️ Курьер: @{courier_username}\n"
                f"📍 Откуда (ПВЗ): {pvz}\n"
                f"🏠 Куда везти: {address}\n"
                f"📞 Телефон клиента: {phone}\n"
                f"💰 Оплата: *{payment_method}*\n"
                f"📊 Статус: 🚴 В работе"
            )
            
            await callback.message.edit_caption(
                caption=updated_text,
                parse_mode="Markdown",
                reply_markup=done_inline_kb
            )
            
    await callback.answer("Вы успешно взяли заказ!")

# Хэндлер нажатия кнопки "Доставлено" курьером
@dp.callback_query(F.data.startswith("status_done_"))
async def handle_done_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    clicker_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, courier_id, courier_username, pvz_address, delivery_address, payment_method FROM orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                user_id, courier_id, courier_username, pvz, address, payment_method = row
                
                # Защита: закрыть заказ может только тот курьер, который его взял
                if clicker_id != courier_id:
                    await callback.answer("Этот заказ везёте не вы! Кнопка заблокирована.", show_alert=True)
                    return
                
                # Обновляем статус в БД
                await db.execute("UPDATE orders SET status = 'Доставлен' WHERE id = ?", (order_id,))
                await db.commit()
                
                # Уведомляем клиента об успешном окончании
                try:
                    await bot.send_message(chat_id=user_id, text="🎉 Ваш заказ успешно доставлен! Спасибо, что выбрали нас.")
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю: {e}")
                
                # Финальный текст в чате курьеров (кнопки полностью удаляются)
                final_text = (
                    f"✅ **ЗАКАЗ #{order_id} ВЫПОЛНЕН**\n\n"
                    f"💪 Доставил: @{courier_username}\n"
                    f"📍 Было зафиксировано ПВЗ: {pvz}\n"
                    f"🏠 Куда отвезли: {address}\n"
                    f"💰 Тип оплаты: {payment_method}\n"
                    f"📊 Статус: 🎉 Доставлен. Сверьте баланс!"
                )
                
                await callback.message.edit_caption(
                    caption=final_text,
                    parse_mode="Markdown",
                    reply_markup=None
                )
                
    await callback.answer("Заказ успешно закрыт!")

# Главная функция запуска
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())