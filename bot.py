import os
import sqlite3
from datetime import datetime, timedelta, time as dtime
from decimal import Decimal

import pandas as pd
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove

from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET

# =============== CONFIG (замени/используй .env) ===============
TG_TOKEN = os.getenv("TG_TOKEN", "YOUR_TG_TOKEN")  # 🔑 токен Telegram бота (BotFather)
REPORT_TZ_NAME = os.getenv("REPORT_TZ", "Europe/Berlin")  # для будущих отчётов/времени
# ===============================================================

bot = Bot(token=TG_TOKEN)
dp = Dispatcher(bot)

# ------------------- SQLite -------------------
conn = sqlite3.connect("trades.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    mode TEXT DEFAULT 'signal',        -- signal | auto
    binance_api_key TEXT,
    binance_api_secret TEXT,
    use_testnet INTEGER DEFAULT 1,     -- 1=testnet, 0=mainnet
    depo REAL DEFAULT 0,
    risk REAL DEFAULT 1,
    limits_daily REAL DEFAULT 5,
    limits_weekly REAL DEFAULT 15,
    limits_max_trades INTEGER DEFAULT 20
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    symbol TEXT,
    entry REAL,
    tp REAL,
    sl REAL,
    volume REAL,
    status TEXT DEFAULT 'open',  -- open|win|loss|signal_open
    exit REAL,
    pnl REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP
)
""")
conn.commit()

# ------------------- Helpers -------------------
def get_user(user_id: int):
    cur.execute("SELECT user_id, mode, binance_api_key, binance_api_secret, use_testnet, depo, risk, limits_daily, limits_weekly, limits_max_trades FROM users WHERE user_id=?",
                (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return get_user(user_id)
    keys = ["user_id","mode","api_key","api_secret","use_testnet","depo","risk","limit_daily","limit_weekly","limit_max_trades"]
    return dict(zip(keys, row))

def set_user(user_id: int, **kwargs):
    fields = []
    values = []
    for k,v in kwargs.items():
        fields.append(f"{k}=?")
        values.append(v)
    values.append(user_id)
    cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", values)
    conn.commit()

def _get_symbol_filters(client: Client, symbol: str):
    info = client.get_symbol_info(symbol)
    if not info:
        raise ValueError(f"Символ {symbol} не найден на Binance")
    f = {x['filterType']: x for x in info['filters']}
    lot = Decimal(f['LOT_SIZE']['stepSize'])
    tick = Decimal(f['PRICE_FILTER']['tickSize'])
    return lot, tick

def _round_qty(qty: float, step: Decimal) -> Decimal:
    q = Decimal(str(qty))
    return (q // step) * step if step != 0 else q

def _round_price(price: float, tick: Decimal) -> Decimal:
    p = Decimal(str(price))
    return (p // tick) * tick if tick != 0 else p

def get_user_client(u: dict) -> Client:
    """Создаёт Binance client c ключами пользователя (для авто‑трейда)."""
    if not u["api_key"] or not u["api_secret"]:
        raise RuntimeError("Не заданы Binance API ключи.")
    return Client(u["api_key"], u["api_secret"], testnet=bool(u["use_testnet"]))

def user_get_price(client: Client, symbol: str) -> float:
    return float(client.get_symbol_ticker(symbol=symbol)['price'])

def user_get_balance(client: Client, asset: str="USDT") -> float:
    bal = client.get_asset_balance(asset=asset)
    return float(bal['free']) if bal else 0.0

def save_trade(uid, symbol, entry, tp, sl, vol, status="open"):
    cur.execute("""INSERT INTO trades (user_id, symbol, entry, tp, sl, volume, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uid, symbol, entry, tp, sl, vol, status))
    conn.commit()
    cur.execute("SELECT last_insert_rowid()")
    return cur.fetchone()[0]

def close_trade_db(trade_id: int, exit_price: float, pnl: float, status: str):
    cur.execute("""UPDATE trades
                   SET status=?, exit=?, pnl=?, closed_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (status, exit_price, pnl, trade_id))
    conn.commit()

def df_user_trades(uid: int) -> pd.DataFrame:
    q = """SELECT id, symbol, entry, tp, sl, volume, status, exit, pnl, created_at, closed_at
           FROM trades WHERE user_id=?
           ORDER BY COALESCE(closed_at, created_at)"""
    return pd.read_sql_query(q, conn, params=(uid,))

# ---------------- Risk Management ----------------
def today_bounds_utc():
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    return start, end

def week_bounds_utc():
    now = datetime.utcnow()
    start = datetime.combine((now - timedelta(days=now.weekday())).date(), dtime.min)
    end = start + timedelta(days=7)
    return start, end

def get_period_pnl(uid, start_dt_utc: datetime, end_dt_utc: datetime) -> float:
    cur.execute("""SELECT COALESCE(SUM(pnl),0) FROM trades
                   WHERE user_id=? AND status IN ('win','loss') AND closed_at BETWEEN ? AND ?""",
                (uid, start_dt_utc.isoformat(" "), end_dt_utc.isoformat(" ")))
    return float(cur.fetchone()[0] or 0.0)

def get_trades_today(uid):
    cur.execute("""SELECT COUNT(*) FROM trades
                   WHERE user_id=? AND DATE(created_at)=DATE('now')""", (uid,))
    return cur.fetchone()[0] or 0

def check_limits(u: dict):
    depo = float(u["depo"] or 0)
    if depo <= 0:
        return True, ""
    start, end = today_bounds_utc()
    pnl_day = get_period_pnl(u["user_id"], start, end)
    day_pct = (pnl_day / depo) * 100.0
    if day_pct <= -abs(u["limit_daily"]):
        return False, "⛔ Дневной лимит просадки достигнут."

    wstart, wend = week_bounds_utc()
    pnl_week = get_period_pnl(u["user_id"], wstart, wend)
    week_pct = (pnl_week / depo) * 100.0
    if week_pct <= -abs(u["limit_weekly"]):
        return False, "⛔ Недельный лимит просадки достигнут."

    if get_trades_today(u["user_id"]) >= int(u["limit_max_trades"]):
        return False, "⛔ Достигнут лимит сделок на сегодня."
    return True, ""

# ================== Start / Mode select ==================
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)

    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("📩 Сигналы", "🤖 Авто-трейд")
    await message.answer(
        "Привет! Выбери режим работы бота:\n"
        "• 📩 Сигналы — только рекомендации, без реальных ордеров\n"
        "• 🤖 Авто‑трейд — реальные ордера через Binance (нужны API ключи)",
        reply_markup=kb
    )

@dp.message_handler(lambda m: m.text in ["📩 Сигналы", "🤖 Авто-трейд"])
async def set_mode(message: types.Message):
    uid = message.from_user.id
    if "Сигналы" in message.text:
        set_user(uid, mode="signal")
        await message.answer("✅ Режим «Сигналы» активирован. Буду записывать сделки в журнал без реальной торговли.",
                             reply_markup=ReplyKeyboardRemove())
    else:
        set_user(uid, mode="auto")
        kb = ReplyKeyboardRemove()
        await message.answer("🤖 Режим «Авто‑трейд». Пришли *Binance API Key* сообщением.",
                             reply_markup=kb, parse_mode="Markdown")

@dp.message_handler(lambda m: True)
async def capture_keys(message: types.Message):
    """Простая пошаговая логика ввода ключей для режима auto."""
    uid = message.from_user.id
    u = get_user(uid)

    # ожидаем API ключ, если auto и ключей ещё нет
    if u["mode"] == "auto" and not u["api_key"]:
        if len(message.text.strip()) < 10:
            await message.answer("⚠️ Похоже, это не API Key. Отправь корректный Binance API Key.")
            return
        set_user(uid, binance_api_key=message.text.strip())
        await message.answer("Отлично! Теперь отправь *Binance API Secret* сообщением.",
                             parse_mode="Markdown")
        return

    if u["mode"] == "auto" and u["api_key"] and not u["api_secret"]:
        if len(message.text.strip()) < 10:
            await message.answer("⚠️ Похоже, это не API Secret. Отправь корректный Binance API Secret.")
            return
        set_user(uid, binance_api_secret=message.text.strip())
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("Testnet", "Mainnet")
        await message.answer("Выбери режим Binance для торговли:", reply_markup=kb)
        return

    if u["mode"] == "auto" and u["api_key"] and u["api_secret"] and message.text in ["Testnet", "Mainnet"]:
        use_testnet = 1 if message.text == "Testnet" else 0
        set_user(uid, use_testnet=use_testnet)
        await message.answer("✅ Ключи сохранены. Можно торговать командами.\n"
                             "Подсказка: /help", reply_markup=ReplyKeyboardRemove())
        return

    # если это не ввод ключей — кинем help при /help
    if message.text == "/help":
        await send_help(message)
        return

# ------------------- Commands core -------------------
@dp.message_handler(commands=['help'])
async def send_help(message: types.Message):
    await message.answer(
        "Команды:\n"
        "/set_depo 1000 — задать депозит (виртуальный)\n"
        "/set_risk 2 — риск на сделку (%)\n"
        "/set_limits daily=5 weekly=15 max_trades=20 — лимиты риска\n"
        "/risk_limits — показать текущие лимиты\n\n"
        "Торговля:\n"
        "/new_trade BTCUSDT 30000 32000 29000 — открыть (в signal: только запись)\n"
        "/close_trade <id> <win|loss> <exit_price> — закрыть сделку вручную\n"
        "/balance — балансы (только auto)\n"
        "/cancel_all BTCUSDT — отменить ордера (auto)\n\n"
        "Отчётность:\n"
        "/report — краткая статистика\n"
        "/equity — график equity\n"
        "/export_csv — CSV\n"
        "/export_xlsx — Excel\n"
    )

@dp.message_handler(commands=['set_depo'])
async def set_depo_cmd(message: types.Message):
    try:
        val = float(message.get_args())
        uid = message.from_user.id
        set_user(uid, depo=val)
        await message.answer(f"💰 Депозит установлен: {val:.2f} USDT")
    except:
        await message.answer("⚠️ Пример: /set_depo 1000")

@dp.message_handler(commands=['set_risk'])
async def set_risk_cmd(message: types.Message):
    try:
        val = float(message.get_args())
        uid = message.from_user.id
        set_user(uid, risk=val)
        await message.answer(f"⚖️ Риск на сделку: {val:.2f}%")
    except:
        await message.answer("⚠️ Пример: /set_risk 2")

@dp.message_handler(commands=['set_limits'])
async def set_limits_cmd(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)
    parts = (message.get_args() or "").split()
    try:
        for p in parts:
            if p.startswith("daily="): set_user(uid, limits_daily=float(p.split("=",1)[1]))
            elif p.startswith("weekly="): set_user(uid, limits_weekly=float(p.split("=",1)[1]))
            elif p.startswith("max_trades="): set_user(uid, limits_max_trades=int(p.split("=",1)[1]))
        u = get_user(uid)
        await message.answer(f"🛡️ Лимиты обновлены: "
                             f"day {u['limit_daily']}% | week {u['limit_weekly']}% | max/day {u['limit_max_trades']}")
    except Exception:
        await message.answer("⚠️ Пример: /set_limits daily=5 weekly=15 max_trades=20")

@dp.message_handler(commands=['risk_limits'])
async def risk_limits_cmd(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)
    depo = float(u["depo"] or 0)
    if depo <= 0:
        await message.answer("Сначала задай депозит: /set_depo 1000")
        return
    dstart, dend = today_bounds_utc()
    wstart, wend = week_bounds_utc()
    d = (get_period_pnl(uid, dstart, dend) / depo) * 100.0
    w = (get_period_pnl(uid, wstart, wend) / depo) * 100.0
    t = get_trades_today(uid)
    await message.answer(
        "🛡️ Лимиты риска:\n"
        f"Daily: {u['limit_daily']}% | Текущий день: {d:.2f}%\n"
        f"Weekly: {u['limit_weekly']}% | Текущая неделя: {w:.2f}%\n"
        f"Max trades/day: {u['limit_max_trades']} | Сегодня: {t}"
    )

@dp.message_handler(commands=['balance'])
async def balance_cmd(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)
    if u["mode"] != "auto":
        await message.answer("ℹ️ Баланс доступен только в режиме Авто‑трейд.")
        return
    try:
        client = get_user_client(u)
        account = client.get_account()
        lines = ["💰 Балансы:"]
        for b in account["balances"]:
            total = float(b["free"]) + float(b["locked"])
            if total > 0:
                lines.append(f"• {b['asset']}: {total}")
        await message.answer("\n".join(lines))
    except Exception as e:
        await message.answer(f"⛔ Ошибка получения балансов: {e}")

@dp.message_handler(commands=['cancel_all'])
async def cancel_all_cmd(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)
    if u["mode"] != "auto":
        await message.answer("ℹ️ Отмена ордеров доступна только в режиме Авто‑трейд.")
        return
    try:
        symbol = message.get_args().split()[0].upper()
        client = get_user_client(u)
        res = client.cancel_open_orders(symbol=symbol)
        await message.answer(f"🚫 Отменены все ордера по {symbol}\n{res}")
    except Exception as e:
        await message.answer(f"⛔ Ошибка отмены: {e}")

@dp.message_handler(commands=['new_trade'])
async def new_trade_cmd(message: types.Message):
    """
    /new_trade SYMBOL ENTRY TP SL
    """
    try:
        uid = message.from_user.id
        u = get_user(uid)

        # lim checks
        ok, reason = check_limits(u)
        if not ok:
            await message.answer(reason)
            return

        symbol, entry, tp, sl = message.get_args().split()
        symbol = symbol.upper()
        entry = float(entry); tp = float(tp); sl = float(sl)

        risk_pct = float(u["risk"] or 0)
        depo = float(u["depo"] or 0)
        if risk_pct <= 0 or depo <= 0:
            await message.answer("⚠️ Сначала задай депозит и риск: /set_depo и /set_risk")
            return

        stop_distance = abs(entry - sl)
        if stop_distance <= 0:
            await message.answer("⚠️ Некорректный SL.")
            return

        risk_amount = depo * (risk_pct / 100.0)
        raw_volume = risk_amount / stop_distance

        if u["mode"] == "signal":
            # Только запись сигнала (без Binance)
            trade_id = save_trade(uid, symbol, entry, tp, sl, raw_volume, status="signal_open")
            await message.answer(f"📝 Сигнал сохранён #{trade_id} {symbol}\n"
                                 f"entry={entry} TP={tp} SL={sl} vol≈{raw_volume:.6f}")
            return

        # AUTO MODE: реальная торговля
        client = get_user_client(u)
        lot_step, tick = _get_symbol_filters(client, symbol)
        qty = _round_qty(raw_volume, lot_step)
        entry_r = float(_round_price(entry, tick))
        tp_r = float(_round_price(tp, tick))
        sl_r = float(_round_price(sl, tick))

        # проверка баланса
        last_price = user_get_price(client, symbol)
        quote_needed = float(qty) * last_price
        free_usdt = user_get_balance(client, "USDT")
        if quote_needed > free_usdt:
            await message.answer(f"⚠️ Недостаточно USDT: нужно {quote_needed:.2f}, доступно {free_usdt:.2f}")
            return

        # MARKET BUY
        order = client.create_order(
            symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=str(qty)
        )

        # средняя цена из fills (если биржа вернула)
        avg_entry = entry_r
        if 'fills' in order and order['fills']:
            exec_qty, exec_quote = Decimal('0'), Decimal('0')
            for f in order['fills']:
                exec_qty += Decimal(f['qty'])
                exec_quote += Decimal(f['price']) * Decimal(f['qty'])
            if exec_qty > 0:
                avg_entry = float(exec_quote / exec_qty)

        # OCO SELL (TP/SL)
        stop_limit_price = float(_round_price(sl_r * 0.999, tick))
        client.create_oco_order(
            symbol=symbol, side=SIDE_SELL, quantity=str(qty),
            price=str(tp_r), stopPrice=str(sl_r),
            stopLimitPrice=str(stop_limit_price), stopLimitTimeInForce="GTC"
        )

        trade_id = save_trade(uid, symbol, avg_entry, tp_r, sl_r, float(qty), status="open")
        await message.answer(f"✅ Открыто #{trade_id} {symbol}\nqty={qty} entry≈{avg_entry:.8f} TP={tp_r} SL={sl_r}")

    except Exception as e:
        await message.answer(f"⛔ Ошибка: {e}\nПример: /new_trade BTCUSDT 30000 32000 29000")

@dp.message_handler(commands=['close_trade'])
async def close_trade_cmd(message: types.Message):
    """
    /close_trade ID win|loss EXIT_PRICE
    """
    try:
        args = message.get_args().split()
        trade_id = int(args[0]); status = args[1].lower(); exit_price = float(args[2])
        if status not in ("win", "loss"):
            await message.answer("⚠️ Статус: win или loss")
            return
        cur.execute("SELECT user_id, entry, volume FROM trades WHERE id=?", (trade_id,))
        row = cur.fetchone()
        if not row:
            await message.answer("⚠️ Сделка не найдена")
            return
        uid, entry, vol = row
        pnl = (exit_price - float(entry)) * float(vol)
        close_trade_db(trade_id, exit_price, pnl, status)
        # обновим виртуальный депо
        u = get_user(uid)
        set_user(uid, depo=float(u["depo"] or 0) + pnl)
        await message.answer(f"✅ Закрыта #{trade_id} ({status}) exit={exit_price} | PnL={pnl:.2f}")
    except Exception:
        await message.answer("⚠️ Пример: /close_trade 12 win 31500")

# ---------------- Отчётность ----------------
@dp.message_handler(commands=['report'])
async def report_cmd(message: types.Message):
    uid = message.from_user.id
    df = df_user_trades(uid)
    if df.empty:
        await message.answer("Пока нет сделок.")
        return
    closed = df[df['status'].isin(['win','loss'])]
    total_trades = len(df); closed_trades = len(closed)
    wins = (closed['pnl'] > 0).sum(); losses = (closed['pnl'] <= 0).sum()
    total_pnl = float(closed['pnl'].sum()) if closed_trades else 0.0
    avg_win = float(closed[closed['pnl'] > 0]['pnl'].mean() or 0.0) if wins else 0.0
    avg_loss = float(closed[closed['pnl'] <= 0]['pnl'].mean() or 0.0) if losses else 0.0
    winrate = (wins / closed_trades * 100.0) if closed_trades else 0.0
    u = get_user(uid)
    text = ( "📊 Отчёт\n"
             f"Всего: {total_trades} | Закрыто: {closed_trades}\n"
             f"🏆 {wins} | ❌ {losses} | Winrate: {winrate:.2f}%\n"
             f"💵 Total PnL: {total_pnl:.2f}\n"
             f"📈 Avg Win: {avg_win:.2f} | 📉 Avg Loss: {avg_loss:.2f}\n"
             f"💰 Депозит (вирт.): {float(u['depo'] or 0):.2f} USDT" )
    await message.answer(text)

@dp.message_handler(commands=['equity'])
async def equity_cmd(message: types.Message):
    uid = message.from_user.id
    df = df_user_trades(uid)
    closed = df[df['status'].isin(['win','loss'])].copy()
    if closed.empty:
        await message.answer("Нет закрытых сделок для графика.")
        return
    closed['closed_at'] = pd.to_datetime(closed['closed_at'])
    closed = closed.sort_values('closed_at')
    closed['cum_pnl'] = closed['pnl'].cumsum()
    y = closed['cum_pnl']
    plt.figure(figsize=(7,4.5))
    plt.plot(closed['closed_at'], y, marker="o")
    plt.title("Equity Curve (Cumulative PnL)")
    plt.xlabel("Дата")
    plt.ylabel("USDT")
    plt.grid(True)
    plt.tight_layout()
    img = f"equity_{uid}.png"
    plt.savefig(img); plt.close()
    await message.answer_photo(open(img, "rb"))

@dp.message_handler(commands=['export_csv'])
async def export_csv_cmd(message: types.Message):
    uid = message.from_user.id
    df = df_user_trades(uid)
    if df.empty:
        await message.answer("Нет сделок для экспорта.")
        return
    path = f"trades_{uid}.csv"
    df.to_csv(path, index=False, encoding="utf-8")
    await message.answer_document(open(path, "rb"))

@dp.message_handler(commands=['export_xlsx'])
async def export_xlsx_cmd(message: types.Message):
    uid = message.from_user.id
    df = df_user_trades(uid)
    if df.empty:
        await message.answer("Нет сделок для экспорта.")
        return
    df['risk_R'] = (df['entry'] - df['sl'])
    df['reward_R'] = (df['tp'] - df['entry'])
    df['rr_ratio'] = df['reward_R'] / df['risk_R'].replace(0, pd.NA)
    path = f"trades_{uid}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Trades")
        closed = df[df['status'].isin(['win','loss'])]
        summary = {
            "total_trades": [len(df)],
            "closed_trades": [len(closed)],
            "wins": [(closed['pnl'] > 0).sum()],
            "losses": [(closed['pnl'] <= 0).sum()],
            "winrate_%": [((closed['pnl'] > 0).sum() / len(closed) * 100) if len(closed) else 0.0],
            "total_pnl": [float(closed['pnl'].sum()) if len(closed) else 0.0],
        }
        pd.DataFrame(summary).to_excel(w, index=False, sheet_name="Summary")
    await message.answer_document(open(path, "rb"))

# ------------------- Run -------------------
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
