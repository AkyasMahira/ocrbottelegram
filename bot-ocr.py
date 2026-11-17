import asyncio, os, re, tempfile

import pypdfium2 as pdfium
from docx import Document
from rapidocr_onnxruntime import RapidOCR
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
SCALE = 2.5  # ~180 dpi; naikkan kalau hasil kurang akurat (RAM ikut naik)
HEADING = re.compile(r"^(bab|chapter)\s+\w+", re.I)

engine = RapidOCR()


def ocr_page(img):
    res, _ = engine(img)
    if not res:
        return []
    lines = sorted((b[0][1], b[2][1] - b[0][1], t) for b, t, _ in res)
    med = sorted(h for _, h, _ in lines)[len(lines) // 2]
    paras, prev = [[]], None
    for y, _, t in lines:
        if prev is not None and y - prev > 1.8 * med:
            paras.append([])
        paras[-1].append(t)
        prev = y
    return [" ".join(p) for p in paras if p]


def run_ocr(path, fmt, progress):
    pdf = pdfium.PdfDocument(path)
    n, paras = len(pdf), []
    for i in range(n):
        page = pdf[i]
        paras += ocr_page(page.render(scale=SCALE).to_numpy())
        page.close()
        progress(i + 1, n)
    out = f"{path[:-4]}.{fmt}"
    if fmt == "txt":
        open(out, "w", encoding="utf-8").write("\n\n".join(paras))
    else:
        d = Document()
        for p in paras:
            d.add_heading(p, 1) if HEADING.match(p) and len(p) < 60 else d.add_paragraph(p)
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


async def on_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        return
    if doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text("File >20 MB, batas download bot. Pecah dulu.")
    fmt = ctx.user_data.get("fmt", "docx")
    msg = await update.message.reply_text("Mulai OCR...")
    loop = asyncio.get_running_loop()

    def progress(i, n):
        if i % 10 == 0 or i == n:
            asyncio.run_coroutine_threadsafe(msg.edit_text(f"OCR {i}/{n}"), loop)

    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "in.pdf")
            await (await doc.get_file()).download_to_drive(path)
            out = await asyncio.to_thread(run_ocr, path, fmt, progress)
            with open(out, "rb") as fh:
                await update.message.reply_document(fh, filename=f"{doc.file_name[:-4]}.{fmt}")
    except Exception as e:
        await update.message.reply_text(f"Gagal: {e}")


app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("fmt", fmt_cmd))
app.add_handler(MessageHandler(filters.Document.PDF, on_pdf))
app.run_polling()
