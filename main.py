import asyncio, os, re, tempfile, time

import pypdfium2 as pdfium
from docx import Document
from rapidocr_onnxruntime import RapidOCR
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
SCALE = 2.5  # ~180 dpi; naikkan kalau hasil kurang akurat (RAM ikut naik)
HEADING = re.compile(r"^(?:(?:bab|chapter|bagian|part)\s*(?:\d+|[ivxlc]+)\b|prolog|epilog)", re.I)
HEADING_NUM = re.compile(r"^(bab|chapter|bagian|part)\s*(\d+|[ivxlc]+)", re.I)
PROGRESS_INTERVAL = 3  # detik; jeda minimum antar update progress ke Telegram

engine = RapidOCR()


def ocr_page(img):
    res, _ = engine(img)
    if not res:
        return []
    lines = sorted(((b[0][1] + b[2][1]) / 2, b[2][1] - b[0][1], t) for b, t, _ in res)
    med = sorted(h for _, h, _ in lines)[len(lines) // 2]
    paras, prev = [[False, []]], None
    for idx, (y, h, t) in enumerate(lines):
        next_gap = lines[idx + 1][0] - y if idx + 1 < len(lines) else None
        last_char = t.rstrip()[-1:] if t.rstrip() else ""
        is_big = (
            h >= 1.15 * med
            and len(t) < 60
            and last_char not in ".,?!\"'‘’“”"
            and not t.strip().isdigit()
            and (next_gap is None or next_gap >= 2.5 * med)
        )
        if os.environ.get("OCR_DEBUG"):
            gap = round((y - prev) / med, 2) if prev is not None else None
            print(gap, round(h / med, 2), t[:25])
        if is_big:
            if paras[-1][1]:
                paras.append([False, []])
            paras[-1] = [True, [t]]
            paras.append([False, []])
        else:
            if prev is not None and y - prev > 2.0 * med:
                paras.append([False, []])
            paras[-1][1].append(t)
        prev = y
    return [(" ".join(p), big) for big, p in paras if p]


def normalize_heading(text):
    return HEADING_NUM.sub(lambda m: f"{m.group(1)} {m.group(2)}", text, count=1)


def is_heading(text, big, heading):
    mode, pattern = heading
    if mode == "off":
        return False
    if mode == "regex":
        return bool(pattern.search(text))
    return big or (HEADING.match(text) and len(text) < 60)


def format_duration(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}j {m}m {s}dtk" if h else f"{m}m {s}dtk"


def run_ocr(path, fmt, progress, heading):
    pdf = pdfium.PdfDocument(path)
    n, paras = len(pdf), []
    for i in range(n):
        page = pdf[i]
        paras += ocr_page(page.render(scale=SCALE).to_numpy())
        page.close()
        progress(i + 1, n)
    out = f"{path[:-4]}.{fmt}"
    if fmt == "txt":
        open(out, "w", encoding="utf-8").write("\n\n".join(t for t, _ in paras))
    else:
        d = Document()
        for t, big in paras:
            if is_heading(t, big, heading):
                d.add_heading(normalize_heading(t), 1)
            else:
                d.add_paragraph(t)
        d.save(out)
    return out


async def fmt_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    f = (ctx.args[0].lower() if ctx.args else "")
    if f not in ("txt", "docx"):
        return await update.message.reply_text("Pakai: /fmt txt atau /fmt docx")
    ctx.user_data["fmt"] = f
    await update.message.reply_text(f"Output: {f}")


async def heading_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    mode = (ctx.args[0].lower() if ctx.args else "")
    if mode == "auto":
        ctx.user_data["heading"] = ("auto", None)
    elif mode == "off":
        ctx.user_data["heading"] = ("off", None)
    elif mode == "regex":
        pat = " ".join(ctx.args[1:])
        try:
            compiled = re.compile(pat, re.I)
        except re.error as e:
            return await update.message.reply_text(f"Regex tidak valid: {e}")
        ctx.user_data["heading"] = ("regex", compiled)
    else:
        return await update.message.reply_text("Pakai: /heading auto|off|regex <pola>")
    await update.message.reply_text(f"Heading: {mode}")


async def on_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        return
    if doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text("File >20 MB, batas download bot. Pecah dulu.")
    fmt = ctx.user_data.get("fmt", "docx")
    heading = ctx.user_data.get("heading", ("auto", None))
    msg = await update.message.reply_text("Mulai OCR...")
    loop = asyncio.get_running_loop()
    last_update = 0.0
    start = 0.0

    def progress(i, n):
        nonlocal last_update
        now = time.time()
        if now - last_update < PROGRESS_INTERVAL and i != n:
            return
        last_update = now
        elapsed = now - start
        pct = i * 100 // n
        if i == n:
            text = f"OCR {i}/{n} (100%) selesai dalam {format_duration(elapsed)}"
        elif i < 3:
            text = f"OCR {i}/{n} ({pct}%)"
        else:
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate if rate > 0 else 0
            text = f"OCR {i}/{n} ({pct}%) • ETA {format_duration(eta)}"
        asyncio.run_coroutine_threadsafe(msg.edit_text(text), loop)

    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "in.pdf")
            await (await doc.get_file()).download_to_drive(path)
            start = time.time()
            out = await asyncio.to_thread(run_ocr, path, fmt, progress, heading)
            with open(out, "rb") as fh:
                await update.message.reply_document(fh, filename=f"{doc.file_name[:-4]}.{fmt}")
    except Exception as e:
        await update.message.reply_text(f"Gagal: {e}")


app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("fmt", fmt_cmd))
app.add_handler(CommandHandler("heading", heading_cmd))
app.add_handler(MessageHandler(filters.Document.PDF, on_pdf))
app.run_polling()
