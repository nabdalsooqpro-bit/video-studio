"""
استوديو نبض للأطفال: أداة خاصة بصاحب المنصة لصناعة حلقات رسوم متحركة تعليمية لقناته على يوتيوب.

الخطوات لكل حلقة:
1. Gemini يكتب القصة مشهداً مشهداً (جملة الراوي + الكلمة التعليمية + وصف الصورة) ومعها عنوان ووصف يوتيوب.
2. Gemini يرسم ورقة الشخصية مرة واحدة (تُحفظ وتُستعمل في كل الحلقات القادمة)، ثم يرسم كل مشهد بنفس الشخصية.
3. Gemini يقرأ جمل الراوي بصوت طبيعي.
4. ffmpeg يركّب الفيلم: حركة كاميرا على كل صورة + بطاقة الكلمة التعليمية + الصوت + موسيقى هادئة،
   ويصنع صورة مصغّرة لليوتيوب وملف ترجمة SRT، ثم يرسل الحلقة لصاحب المنصة في تيليجرام.

الإعداد في Render:
- GEMINI_KEY: مفتاح Google AI Studio (مع تفعيل الدفع).
- STUDIO_PASSWORD: كلمة سر دخول الاستوديو.
- اختياري: TELEGRAM_TOKEN و TELEGRAM_CHAT_ID لإرسال كل حلقة جاهزة لتيليجرام.
- اختياري: STUDIO_TEXT_MODEL, STUDIO_IMG_MODEL, STUDIO_TTS_MODEL لتغيير النماذج.
- STUDIO_MOCK=1 للاختبار بلا مفاتيح (صور وأصوات تجريبية).
خادم مستقل تماماً عن موقع نبض (Hugging Face Space مجاني).
"""
import asyncio
import base64
import json
import math
import os
import random
import re
import shutil
import time
import uuid
import wave
from pathlib import Path

import httpx
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features


router = APIRouter()
ROOT = Path(os.getenv("STUDIO_DIR", "/data/studio" if os.path.isdir("/data") else "/tmp/nabd_studio"))
ASSETS = Path(__file__).parent / "studio_assets"
GEM = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODEL = os.getenv("STUDIO_TEXT_MODEL", "gemini-2.5-flash")
IMG_MODEL = os.getenv("STUDIO_IMG_MODEL", "gemini-2.5-flash-image")
TTS_MODEL = os.getenv("STUDIO_TTS_MODEL", "gemini-2.5-flash-preview-tts")
MOCK = os.getenv("STUDIO_MOCK", "") == "1"
FPS = 25
SEC_PER_SCENE = 7.5
STYLE = ("Children's animated cartoon still, soft 3D animation style, bright cheerful colors, warm gentle lighting, "
         "cute rounded friendly shapes, clean background, high detail, safe and calm for preschool kids")
VOICES = {"Kore": "راوية دافئة", "Leda": "راوية شابة مرحة", "Puck": "راوٍ مرح", "Charon": "راوٍ هادئ رخيم"}

JOBS: dict = {}
QUEUE: asyncio.Queue | None = None
WORKER = None


class StudioErr(Exception):
    pass


# ---------------- مساعدات عامة ----------------
def _key() -> str:
    return (os.getenv("GEMINI_KEY") or os.getenv("STUDIO_GEMINI_KEY") or "").strip()


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# تخزين بسيط في ملفات (بدل قاعدة بيانات الموقع)
def _kvp(k):
    return ROOT / "kv" / (re.sub(r"[^0-9A-Za-z_.-]", "_", k) + ".json")


async def _kv_get(k, d=None):
    try:
        return json.loads(_kvp(k).read_text())
    except Exception:  # noqa: BLE001
        return d


async def _kv_set(k, v):
    _kvp(k).parent.mkdir(parents=True, exist_ok=True)
    tmp = _kvp(k).with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False))
    os.replace(tmp, _kvp(k))


def _save_jobs():
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "jobs.json").write_text(json.dumps(JOBS, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _load_jobs():
    try:
        JOBS.update(json.loads((ROOT / "jobs.json").read_text()))
        for j in JOBS.values():
            if j["status"] in ("queued", "running"):
                j.update(status="error", error="توقف الخادم أثناء العمل (تحديث أو إعادة تشغيل). أعد إرسال الحلقة.")
    except Exception:  # noqa: BLE001
        pass


def _step(job, text, pct):
    job.update(step=text, pct=int(pct))
    _save_jobs()


# ---------------- Gemini ----------------
async def _gem(model: str, body: dict, timeout=120) -> dict:
    key = _key()
    if not key:
        raise StudioErr("لا يوجد مفتاح Gemini. أضف STUDIO_GEMINI_KEY في Render.")
    last = ""
    for a in range(5):
        try:
            async with httpx.AsyncClient(timeout=timeout) as cl:
                r = await cl.post(f"{GEM}/{model}:generateContent", json=body, headers={"x-goog-api-key": key})
            if r.status_code == 200:
                return r.json()
            try:
                last = r.json().get("error", {}).get("message", "")[:300]
            except Exception:  # noqa: BLE001
                last = r.text[:300]
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(6 * (a + 1) + random.random() * 3)
                continue
            if r.status_code in (401, 403):
                raise StudioErr(f"مفتاح Gemini مرفوض ({r.status_code}): {last}")
            if r.status_code == 404:
                raise StudioErr(f"النموذج {model} غير متاح لهذا المفتاح: {last}")
            raise StudioErr(f"خطأ من Gemini ({r.status_code}): {last}")
        except httpx.HTTPError as e:
            last = str(e)
            await asyncio.sleep(5 * (a + 1))
    raise StudioErr(f"Gemini لا يستجيب بعد عدة محاولات ({model}): {last}")


def _parts(resp: dict) -> list:
    try:
        return resp["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        why = (resp.get("promptFeedback") or {}).get("blockReason") or (resp.get("candidates") or [{}])[0].get("finishReason")
        raise StudioErr(f"Gemini لم يرجع نتيجة ({why or 'سبب غير معروف'})")


def _inline(resp: dict) -> bytes:
    for p in _parts(resp):
        d = p.get("inlineData") or p.get("inline_data")
        if d and d.get("data"):
            return base64.b64decode(d["data"])
    raise StudioErr("Gemini لم يرجع صورة أو صوتاً (ربما رفض الطلب). جرّب موضوعاً أو وصفاً آخر.")


# ---------------- 1) القصة ----------------
async def write_story(p: dict, look: str, past: list) -> dict:
    n = max(6, round(p["minutes"] * 60 / SEC_PER_SCENE))
    lang = "العربية الفصحى البسيطة المناسبة لطفل 4-8 سنوات" if p["lang"] == "ar" else "simple English for kids aged 4-8"
    prompt = f"""أنت كاتب حلقات رسوم متحركة تعليمية لقناة أطفال على يوتيوب.
اكتب حلقة واحدة بلغة: {lang}.
الموضوع التعليمي: {p['topic']}
البطل: {p['hero']} (الشكل: {look})
العالم/المكان: {p.get('world') or 'تختاره أنت بما يناسب الموضوع'}
عدد المشاهد: {n} بالضبط. كل مشهد جملة أو جملتان للراوي (12 إلى 20 كلمة، تُقرأ في 6-8 ثوانٍ).
القواعد:
- تعليم حقيقي وصحيح علمياً ولغوياً، بأسلوب قصة ممتعة، مع تكرار المعلومة الأساسية للحفظ.
- المشهد الأول يرحّب ويعرّف بالبطل والموضوع، والأخير يلخّص ما تعلّمناه ويودّع الأطفال.
- لا عنف، لا خوف، لا مواقف خطرة، لا علامات تجارية، لا شخصيات مشهورة أو محمية.
- "word": الكلمة أو العبارة التعليمية التي تظهر على الشاشة (كلمة إلى 4 كلمات) بلغة الحلقة.
- "img": وصف المشهد بالإنجليزية للرسام: ماذا يفعل البطل، أين، ما الأشياء الظاهرة. بلا أي نص أو حروف داخل الصورة.
عناوين حلقات سابقة (لا تكررها): {', '.join(past[-15:]) or 'لا يوجد'}
أرجع JSON فقط بهذا الشكل:
{{"title": "عنوان الحلقة القصير", "yt_title": "عنوان يوتيوب جذاب (أقل من 90 حرفاً)", "yt_description": "وصف يوتيوب من 3-5 أسطر",
"tags": ["وسم", "..."], "scenes": [{{"say": "...", "word": "...", "img": "..."}}]}}"""
    if MOCK:
        return {"title": f"تعلم {p['topic']}", "yt_title": f"{p['topic']} للأطفال | قصة تعليمية", "yt_description": "حلقة تجريبية.",
                "tags": ["أطفال", "تعليم"], "scenes": [{"say": f"المشهد رقم {i + 1}: {p['hero']} يتعلم {p['topic']} معنا اليوم بفرح.",
                                                       "word": f"{p['topic']} {i + 1}", "img": "kid in a garden"} for i in range(n)]}
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    resp = await _gem(TEXT_MODEL, body, 180)
    txt = "".join(x.get("text", "") for x in _parts(resp))
    try:
        st = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
    except json.JSONDecodeError:
        raise StudioErr("القصة رجعت بصيغة غير صحيحة. أعد المحاولة.")
    sc = [s for s in st.get("scenes", []) if s.get("say") and s.get("img")]
    if len(sc) < 3:
        raise StudioErr("القصة قصيرة جداً. أعد المحاولة.")
    st["scenes"] = sc[: n + 2]
    return st


# ---------------- 2) الصور ----------------
def _mock_img(text: str, ar: str, seed: int) -> bytes:
    rnd = random.Random(seed)
    W, H = (1344, 768) if ar == "16:9" else (768, 1344)
    c1, c2 = [tuple(rnd.randint(60, 230) for _ in range(3)) for _ in range(2)]
    im = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    for _ in range(14):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(30, 140)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randint(120, 255) for _ in range(3)))
    d.ellipse([W / 2 - 120, H / 2 - 220, W / 2 + 120, H / 2 + 20], fill=(255, 220, 180))
    d.rounded_rectangle([W / 2 - 150, H / 2 + 20, W / 2 + 150, H / 2 + 330], 60, fill=(255, 200, 60))
    import io
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


async def draw_char(name: str, look: str) -> bytes:
    prompt = (f"{STYLE}. Character design sheet of ONE original kid character named {name}: {look}. "
              "Full body, front view, friendly smile, standing on a plain soft pastel background. No text, no letters.")
    if MOCK:
        return _mock_img(name, "16:9", hash(name) % 999)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "1:1"}}}
    return _inline(await _gem(IMG_MODEL, body))


async def draw_scene(desc: str, look: str, ref: bytes, ar: str, seed: int) -> bytes:
    if MOCK:
        return _mock_img(desc, ar, seed)
    prompt = (f"{STYLE}. Scene: {desc}. The main kid character must look exactly like the character in the reference image "
              f"({look}): same face, hair, clothes and colors. {'Wide 16:9' if ar == '16:9' else 'Vertical 9:16'} composition, "
              "character clearly visible. Absolutely no text, letters, numbers or words anywhere in the image.")
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(ref).decode()}},
                                    {"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ar}}}
    return _inline(await _gem(IMG_MODEL, body))


# ---------------- 3) الصوت ----------------
def _write_wav(path: Path, pcm: bytes, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def speak(text: str, voice: str, lang: str, path: Path) -> float:
    if MOCK:
        secs = max(2.5, len(text.split()) * 0.42)
        t = np.arange(int(secs * 24000)) / 24000
        sig = (0.08 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)
        _write_wav(path, (sig * 32767).astype(np.int16).tobytes())
        return secs
    style = ("اقرأ بالعربية الفصحى بصوت راوٍ دافئ ومرح لقصص الأطفال، بنطق واضح وبطء خفيف وتشكيل صحيح: "
             if lang == "ar" else "Read as a warm, cheerful kids' storyteller, clear and slightly slow: ")
    body = {"contents": [{"parts": [{"text": style + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    pcm = _inline(await _gem(TTS_MODEL, body))
    _write_wav(path, pcm)
    return len(pcm) / 2 / 24000


# ---------------- 4) التركيب ----------------
def _font(size, bold=True):
    return ImageFont.truetype(str(ASSETS / ("Baloo800.ttf" if bold else "Baloo600.ttf")), size)


RAQM = features.check("raqm")


def _shape(text: str, rtl: bool) -> tuple[str, dict]:
    """نص عربي صحيح الاتصال: raqm إن وُجد، وإلا arabic_reshaper + bidi."""
    if not rtl:
        return text, {}
    if RAQM:
        return text, {"direction": "rtl"}
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(text)), {}


def _wrap(text, font, maxw, rtl):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        s, kw = _shape(t, rtl)
        if font.getlength(s, **kw) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _text_block(d, lines, font, cx, y, rtl, fill, stroke=0, sfill=None, gap=1.15):
    for ln in lines:
        s, kw = _shape(ln, rtl)
        d.text((cx, y), s, font=font, fill=fill, anchor="ma", stroke_width=stroke, stroke_fill=sfill, **kw)
        y += int(font.size * gap)
    return y


def _cover(img: Image.Image, W, H) -> Image.Image:
    r = max(W / img.width, H / img.height)
    img = img.resize((math.ceil(img.width * r), math.ceil(img.height * r)), Image.LANCZOS)
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def word_card(word: str, W, H, rtl, path: Path):
    """بطاقة الكلمة التعليمية أسفل الشاشة."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = _font(int(min(W, H) * 0.085))
    lines = _wrap(word, f, W * 0.8, rtl)[:2]
    tw = max(f.getlength(*[_shape(x, rtl)[0]], **_shape(x, rtl)[1]) for x in lines)
    bh = int(f.size * 1.2 * len(lines) + f.size * 0.55)
    bw = int(tw + f.size * 1.4)
    x0, y0 = (W - bw) // 2, H - bh - int(H * 0.06)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 10, x0 + bw, y0 + bh + 10], bh // 2.4, fill=(0, 0, 0, 110))
    im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], int(bh / 2.4), fill=(255, 214, 74, 255), outline=(255, 255, 255, 255), width=6)
    _text_block(d, lines, f, W // 2, y0 + int(f.size * 0.12), rtl, (35, 30, 80))
    im.save(path)


def title_card(title: str, W, H, rtl, path: Path, sub: str = ""):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(g).rectangle([0, int(H * 0.25), W, int(H * 0.75)], fill=(20, 14, 60, 150))
    im = Image.alpha_composite(im, g.filter(ImageFilter.GaussianBlur(40)))
    d = ImageDraw.Draw(im)
    f = _font(int(min(W, H) * 0.11))
    lines = _wrap(title, f, W * 0.86, rtl)[:3]
    y = H // 2 - int(f.size * 1.15 * len(lines) / 2) - (int(f.size * 0.3) if sub else 0)
    y = _text_block(d, lines, f, W // 2, y, rtl, (255, 214, 74), stroke=max(4, f.size // 14), sfill=(35, 20, 90))
    if sub:
        f2 = _font(int(f.size * 0.45), False)
        _text_block(d, [sub], f2, W // 2, y + 6, rtl, (255, 255, 255), stroke=3, sfill=(35, 20, 90))
    im.save(path)


def thumbnail(img: Path, title: str, rtl: bool, path: Path):
    W, H = 1280, 720
    base = _cover(Image.open(img).convert("RGB"), W, H).convert("RGBA")
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(g).rectangle([0, int(H * 0.58), W, H], fill=(10, 6, 40, 200))
    base = Image.alpha_composite(base, g.filter(ImageFilter.GaussianBlur(30)))
    d = ImageDraw.Draw(base)
    f = _font(96)
    lines = _wrap(title, f, W * 0.9, rtl)[:2]
    _text_block(d, lines, f, W // 2, H - int(f.size * 1.15 * len(lines)) - 40, rtl, (255, 214, 74), stroke=7, sfill=(30, 15, 80))
    base.convert("RGB").save(path, quality=92)


def music(secs: float, path: Path, seed=7):
    """موسيقى هادئة مولّدة (ملك لنا، بلا حقوق): نغمات خماسية ناعمة فوق خلفية دافئة."""
    sr = 24000
    n = int((secs + 1) * sr)
    out = np.zeros(n, np.float32)
    rnd = random.Random(seed)
    beat = 60 / 96
    scale = [0, 2, 4, 7, 9, 12, 14, 16]
    chords = [[0, 4, 7], [-3, 0, 4], [-7, -3, 0], [-5, -1, 2]]
    base = 261.63
    t_ch = np.arange(int(beat * 8 * sr)) / sr
    env_ch = np.minimum(1, t_ch / 0.8) * np.minimum(1, (t_ch[-1] - t_ch) / 0.8)
    for k, start in enumerate(np.arange(0, secs + 1, beat * 8)):
        i0 = int(start * sr)
        seg = sum(np.sin(2 * np.pi * base / 2 * 2 ** (s / 12) * t_ch) for s in chords[k % 4]) * 0.05 * env_ch
        j = min(n, i0 + len(seg))
        out[i0:j] += seg[: j - i0]
    t_n = np.arange(int(beat * 1.6 * sr)) / sr
    env_n = np.exp(-t_n * 5)
    for start in np.arange(0, secs, beat):
        if rnd.random() < 0.55:
            f0 = base * 2 ** (rnd.choice(scale) / 12)
            seg = (np.sin(2 * np.pi * f0 * t_n) + 0.3 * np.sin(4 * np.pi * f0 * t_n)) * 0.09 * env_n
            i0 = int(start * sr)
            j = min(n, i0 + len(seg))
            out[i0:j] += seg[: j - i0]
    out /= max(1e-6, np.abs(out).max()) / 0.6
    _write_wav(path, (out * 32767).astype(np.int16).tobytes(), sr)


async def _run(*args, cwd=None):
    def low():
        try:
            os.nice(15)
        except Exception:  # noqa: BLE001
            pass
    pr = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
                                              stderr=asyncio.subprocess.PIPE, preexec_fn=low)
    _, err = await pr.communicate()
    if pr.returncode != 0:
        raise StudioErr("خطأ في تركيب الفيديو: " + err.decode(errors="ignore")[-400:])


async def render_scene(ff, img: Path, card: Path, wav: Path | None, dur: float, W, H, k: int, out: Path):
    N = max(2, int(dur * FPS))
    big = img.with_suffix(".big.png")
    _cover(Image.open(img).convert("RGB"), W * 2, H * 2).save(big)
    z, Z = "1.0", "0.12"
    mv = [f"z='{z}+{Z}*on/{N}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'",
          f"z='{z}+{Z}-{Z}*on/{N}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'",
          f"z='1.12':x='(iw-iw/zoom)*on/{N}':y='ih/2-ih/zoom/2'",
          f"z='1.12':x='(iw-iw/zoom)*(1-on/{N})':y='ih/2-ih/zoom/2'"][k % 4]
    flt = (f"[0:v]zoompan={mv}:d={N}:s={W}x{H}:fps={FPS},format=yuv420p[bg];"
           f"[1:v]format=rgba,fade=t=in:st=0.35:d=0.4:alpha=1[c];[bg][c]overlay=0:0:shortest=1,format=yuv420p[v]")
    args = [ff, "-y", "-loglevel", "error", "-i", str(big), "-loop", "1", "-t", f"{dur:.2f}", "-i", str(card)]
    if wav:
        args += ["-i", str(wav)]
    else:
        args += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=24000:cl=mono"]
    args += ["-filter_complex", flt + f";[2:a]apad,atrim=0:{dur:.2f},aresample=44100[a]", "-map", "[v]", "-map", "[a]",
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", str(FPS),
             "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(out)]
    await _run(*args)
    big.unlink(missing_ok=True)


def _srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


# ---------------- الشخصيات المحفوظة ----------------
async def chars() -> list:
    return await _kv_get("studio_chars", []) or []


async def char_img(cid: str) -> bytes | None:
    b = await _kv_get(f"studio_charimg:{cid}")
    return base64.b64decode(b) if b else None


async def save_char(name: str, look: str, img: bytes) -> dict:
    c = {"id": uuid.uuid4().hex[:10], "name": name[:40], "look": look[:300], "t": int(time.time())}
    await _kv_set(f"studio_charimg:{c['id']}", base64.b64encode(img).decode())
    await _kv_set("studio_chars", [*(await chars()), c])
    return c


# ---------------- تشغيل حلقة كاملة ----------------
async def make_episode(job: dict):
    p, jd = job["p"], ROOT / job["id"]
    jd.mkdir(parents=True, exist_ok=True)
    rtl = p["lang"] == "ar"
    W, H = (1280, 720) if p["fmt"] == "wide" else (720, 1280)
    ar = "16:9" if p["fmt"] == "wide" else "9:16"
    ff = _ffmpeg()

    # الشخصية: محفوظة أو جديدة (تُحفظ تلقائياً لتبقى نفسها في كل الحلقات)
    _step(job, "تجهيز الشخصية", 3)
    ch = next((c for c in await chars() if c["id"] == p.get("char")), None)
    if ch:
        ref = await char_img(ch["id"])
        name, look = ch["name"], ch["look"]
    else:
        name, look = p["hero"].split("،")[0].split(",")[0].strip()[:40], p["hero"]
        ref = await draw_char(name, look)
        ch = await save_char(name, look, ref)
        job["new_char"] = ch["name"]
    (jd / "char.png").write_bytes(ref)
    p["hero"] = f"{name}: {look}"

    _step(job, "كتابة القصة", 8)
    past = await _kv_get("studio_eps", []) or []
    st = await write_story(p, look, [e.get("title", "") for e in past])
    job["title"] = st.get("title", "")
    job["yt"] = {k: st.get(k) for k in ("yt_title", "yt_description", "tags")}
    sc = st["scenes"]
    (jd / "story.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))

    # الصور والأصوات بالتوازي (3 طلبات معاً حتى لا نتجاوز حدود Gemini)
    sem, done = asyncio.Semaphore(3), [0]
    total = len(sc) * 2

    async def one(i, s):
        async with sem:
            (jd / f"s{i:02}.png").write_bytes(await draw_scene(s["img"], look, ref, ar, i))
            done[0] += 1
            _step(job, f"رسم المشاهد والأصوات ({done[0]}/{total})", 12 + 58 * done[0] / total)
            s["dur"] = await speak(s["say"], p["voice"], p["lang"], jd / f"s{i:02}.wav")
            done[0] += 1
            _step(job, f"رسم المشاهد والأصوات ({done[0]}/{total})", 12 + 58 * done[0] / total)
    await asyncio.gather(*(one(i, s) for i, s in enumerate(sc)))

    # التركيب
    parts = []
    intro = jd / "intro.mp4"
    title_card(st.get("title") or p["topic"], W, H, rtl, jd / "t_intro.png", p.get("channel", ""))
    await render_scene(ff, jd / "s00.png", jd / "t_intro.png", None, 3.5, W, H, 1, intro)
    parts.append(intro)
    t, srt = 3.5, []
    for i, s in enumerate(sc):
        _step(job, f"تركيب الفيلم ({i + 1}/{len(sc)})", 70 + 25 * i / len(sc))
        dur = s["dur"] + 0.8
        word_card(s.get("word") or "", W, H, rtl, jd / f"c{i:02}.png")
        out = jd / f"v{i:02}.mp4"
        await render_scene(ff, jd / f"s{i:02}.png", jd / f"c{i:02}.png", jd / f"s{i:02}.wav", dur, W, H, i, out)
        parts.append(out)
        srt.append(f"{i + 1}\n{_srt_time(t + 0.1)} --> {_srt_time(t + s['dur'] + 0.2)}\n{s['say']}\n")
        t += dur
    (jd / "list.txt").write_text("".join(f"file '{x.name}'\n" for x in parts))
    await _run(ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "raw.mp4", cwd=str(jd))
    _step(job, "إضافة الموسيقى", 96)
    final = jd / "episode.mp4"
    if p.get("music", True):
        music(t, jd / "music.wav", hash(job["id"]) % 1000)
        await _run(ff, "-y", "-loglevel", "error", "-i", "raw.mp4", "-i", "music.wav", "-filter_complex",
                   "[1:a]volume=0.16,aresample=44100,afade=t=out:st=%.2f:d=2[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]" % max(0, t - 2),
                   "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                   final.name, cwd=str(jd))
    else:
        await _run(ff, "-y", "-loglevel", "error", "-i", "raw.mp4", "-c", "copy", "-movflags", "+faststart", final.name, cwd=str(jd))
    (jd / "episode.srt").write_text("\n".join(srt), encoding="utf-8")
    thumbnail(jd / "s00.png", st.get("title") or p["topic"], rtl, jd / "thumb.jpg")
    for x in jd.glob("v*.mp4"):
        x.unlink()
    for x in (jd / "raw.mp4", intro):
        x.unlink(missing_ok=True)
    job["secs"] = round(t)
    job["mb"] = round(final.stat().st_size / 1e6, 1)
    await _kv_set("studio_eps", [*past, {"title": job["title"], "topic": p["topic"], "t": int(time.time())}][-200:])


async def to_telegram(job: dict):
    tok, owner = os.getenv("TELEGRAM_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (tok and owner):
        return "تيليجرام غير مربوط (أضف TELEGRAM_TOKEN و TELEGRAM_CHAT_ID)"
    api = f"https://api.telegram.org/bot{tok}"
    jd, y = ROOT / job["id"], job.get("yt") or {}
    cap = f"🎬 {job.get('title', '')}\n⏱ {job.get('secs', 0)} ثانية"
    try:
        async with httpx.AsyncClient(timeout=300) as cl:
            if (jd / "episode.mp4").stat().st_size < 49e6:
                with open(jd / "episode.mp4", "rb") as f:
                    r = await cl.post(f"{api}/sendVideo", data={"chat_id": owner, "caption": cap, "supports_streaming": "true"},
                                      files={"video": ("episode.mp4", f, "video/mp4")})
                if not r.json().get("ok"):
                    return "تيليجرام رفض الفيديو"
            with open(jd / "thumb.jpg", "rb") as f:
                await cl.post(f"{api}/sendPhoto", data={"chat_id": owner, "caption": "الصورة المصغّرة لليوتيوب"},
                              files={"photo": ("thumb.jpg", f, "image/jpeg")})
            txt = f"📺 عنوان يوتيوب:\n{y.get('yt_title', '')}\n\n📝 الوصف:\n{y.get('yt_description', '')}\n\n🏷 {' ، '.join(y.get('tags') or [])}"
            await cl.post(f"{api}/sendMessage", json={"chat_id": owner, "text": txt[:4000]})
            with open(jd / "episode.srt", "rb") as f:
                await cl.post(f"{api}/sendDocument", data={"chat_id": owner, "caption": "ملف الترجمة لليوتيوب"},
                              files={"document": ("episode.srt", f, "application/x-subrip")})
        return "أُرسلت إلى تيليجرام"
    except Exception as e:  # noqa: BLE001
        return f"تعذر الإرسال لتيليجرام: {e}"


async def _worker():
    while True:
        jid = await QUEUE.get()
        job = JOBS.get(jid)
        if not job:
            continue
        job.update(status="running", started=int(time.time()))
        try:
            if job["p"].get("kind") == "free":
                import freevid
                await freevid.make_video(job)
            else:
                await make_episode(job)
            job.update(status="done", step="جاهزة ✅", pct=100, done=int(time.time()))
            _save_jobs()
            if job["p"].get("tg", True):
                job["tg"] = await to_telegram(job)
        except StudioErr as e:
            job.update(status="error", error=str(e))
        except Exception as e:  # noqa: BLE001
            job.update(status="error", error=f"خطأ غير متوقع: {type(e).__name__}: {str(e)[:300]}")
        _save_jobs()


def _ensure_worker():
    global QUEUE, WORKER
    if QUEUE is None:
        QUEUE = asyncio.Queue()
        _load_jobs()
    if WORKER is None or WORKER.done():
        WORKER = asyncio.create_task(_worker())


# ---------------- الدخول بكلمة سر ----------------
import hashlib
import hmac


def _tok() -> str:
    pw = os.getenv("STUDIO_PASSWORD", "")
    return hmac.new(pw.encode(), b"nabd-studio", hashlib.sha256).hexdigest() if pw else ""


def require_owner(request: Request) -> dict:
    t = _tok()
    if not t:
        raise HTTPException(403, {"message": "أضف STUDIO_PASSWORD في إعدادات الخادم أولاً"})
    if not hmac.compare_digest(request.cookies.get("st", ""), t):
        raise HTTPException(401, {"message": "سجّل الدخول"})
    return {"ok": True}


# ---------------- الواجهة البرمجية ----------------
def _pub(j):
    return {k: j.get(k) for k in ("id", "status", "step", "pct", "error", "title", "yt", "secs", "mb", "created", "tg", "new_char", "note")} | {
        "topic": j["p"]["topic"], "minutes": j["p"]["minutes"], "fmt": j["p"]["fmt"], "kind": j["p"].get("kind", "kids"),
        "visual": j["p"].get("visual")}


@router.get("/api/studio/state")
async def state(_: dict = Depends(require_owner)):
    _ensure_worker()
    import freevid
    return {"key": bool(_key()) or MOCK, "mock": MOCK, "voices": VOICES, "free": freevid.info(),
            "chars": await chars(), "jobs": [_pub(j) for j in sorted(JOBS.values(), key=lambda x: -x["created"])][:30]}


@router.post("/api/studio/jobs")
async def new_job(body: dict, _: dict = Depends(require_owner)):
    _ensure_worker()
    topic = str(body.get("topic", "")).strip()[:200]
    if not topic:
        raise HTTPException(400, {"message": "اكتب موضوع الحلقة"})
    p = {"topic": topic, "hero": str(body.get("hero", "")).strip()[:300], "char": body.get("char") or None,
         "world": str(body.get("world", "")).strip()[:200], "lang": "en" if body.get("lang") == "en" else "ar",
         "minutes": min(5, max(1, int(body.get("minutes") or 1))), "fmt": "tall" if body.get("fmt") == "tall" else "wide",
         "voice": body.get("voice") if body.get("voice") in VOICES else "Kore", "music": bool(body.get("music", True)),
         "tg": bool(body.get("tg", True)), "channel": str(body.get("channel", "")).strip()[:60]}
    if not p["char"] and not p["hero"]:
        raise HTTPException(400, {"message": "اختر شخصية محفوظة أو اكتب وصف البطل"})
    jid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    JOBS[jid] = {"id": jid, "p": p, "status": "queued", "step": "في الانتظار", "pct": 0, "created": int(time.time())}
    _save_jobs()
    await QUEUE.put(jid)
    return _pub(JOBS[jid])


@router.post("/api/studio/jobs/{jid}/again")
async def again(jid: str, _: dict = Depends(require_owner)):
    _ensure_worker()
    j = JOBS.get(jid)
    if not j or j["status"] != "error":
        raise HTTPException(404, {"message": "غير موجودة"})
    j.update(status="queued", step="في الانتظار", pct=0, error=None)
    await QUEUE.put(jid)
    return _pub(j)


@router.delete("/api/studio/jobs/{jid}")
async def drop(jid: str, _: dict = Depends(require_owner)):
    j = JOBS.get(jid)
    if j and j["status"] != "running":
        JOBS.pop(jid, None)
        shutil.rmtree(ROOT / jid, ignore_errors=True)
        _save_jobs()
    return {"ok": True}


@router.delete("/api/studio/chars/{cid}")
async def drop_char(cid: str, _: dict = Depends(require_owner)):
    await _kv_set("studio_chars", [c for c in await chars() if c["id"] != cid])
    return {"ok": True}


FILES = {"episode.mp4": "video/mp4", "thumb.jpg": "image/jpeg", "episode.srt": "application/x-subrip", "char.png": "image/png"}


@router.get("/api/studio/file/{jid}/{name}")
async def file(jid: str, name: str, _: dict = Depends(require_owner)):
    f = ROOT / re.sub(r"[^0-9a-z]", "", jid) / name
    if name not in FILES or not f.exists():
        raise HTTPException(404, {"message": "الملف غير موجود (ربما أعيد تشغيل الخادم)"})
    return FileResponse(f, media_type=FILES[name], filename=None if name.endswith((".mp4", ".jpg", ".png")) else name)


@router.get("/api/studio/charimg/{cid}")
async def charimg(cid: str, _: dict = Depends(require_owner)):
    from fastapi.responses import Response
    b = await char_img(cid)
    if not b:
        raise HTTPException(404, {"message": "غير موجودة"})
    return Response(b, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
