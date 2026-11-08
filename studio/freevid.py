"""
الفيديو الحر (بطلب المالك 8 نوفمبر 2026): أي فيديو يطلبه، أي موضوع وأسلوب وطول، وكل الأدوات مجانية:
- النص: Gemini (الطبقة المجانية، مفتاح مجاني بلا بطاقة).
- الصوت: edge-tts من مايكروسوفت (مجاني بلا مفتاح، أصوات عربية بلهجات). البديل: صوت Gemini.
- الصور: Gemini للصور إن سمحت الطبقة المجانية، وإلا Pollinations (مجاني بلا مفتاح).
- المقاطع الحقيقية المتحركة: Pexels (مجاني بمفتاح مجاني، ويُسمح استخدامها تجارياً).
- التركيب: ffmpeg.
الإعداد (كله اختياري ما عدا مفتاح Gemini):
- PEXELS_KEY: مفتاح Pexels المجاني (لوضع «مقاطع حقيقية»).
- POLLINATIONS_TOKEN: حساب Pollinations المجاني (يرفع الحد ويزيل العلامة المائية).
- STUDIO_TEXT_MODEL / STUDIO_IMG_MODEL: لتغيير النماذج.
"""
import asyncio
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from PIL import Image, ImageDraw, ImageFilter

import studio as S

router = APIRouter()

# أصوات edge-tts المجانية (اسم الصوت: الوصف، اللغة)
VOICES = {
    "ar-SA-HamedNeural": ("حامد · سعودي", "ar"), "ar-SA-ZariyahNeural": ("زارية · سعودية", "ar"),
    "ar-EG-ShakirNeural": ("شاكر · مصري", "ar"), "ar-EG-SalmaNeural": ("سلمى · مصرية", "ar"),
    "ar-AE-HamdanNeural": ("حمدان · إماراتي", "ar"), "ar-AE-FatimaNeural": ("فاطمة · إماراتية", "ar"),
    "en-US-GuyNeural": ("Guy · US", "en"), "en-US-JennyNeural": ("Jenny · US", "en"),
}
STYLES = ["كرتون ثلاثي الأبعاد", "واقعي سينمائي", "أنمي", "رسم مائي", "رسوم مسطحة بسيطة", "فن رقمي ملحمي", "صورة فوتوغرافية", "بكسل آرت"]
STYLE_EN = {"كرتون ثلاثي الأبعاد": "3D animated cartoon still, Pixar-like, bright colors", "واقعي سينمائي": "cinematic photorealistic film still, dramatic lighting",
            "أنمي": "anime illustration, studio quality, vivid", "رسم مائي": "soft watercolor painting",
            "رسوم مسطحة بسيطة": "clean flat vector illustration, minimal", "فن رقمي ملحمي": "epic digital art, highly detailed concept art",
            "صورة فوتوغرافية": "professional photograph, natural light, sharp", "بكسل آرت": "detailed pixel art"}
MINUTES = (0.5, 1, 3, 5, 10)
SEC = 7.0


def info() -> dict:
    import os
    return {"voices": {k: v[0] for k, v in VOICES.items()}, "voice_lang": {k: v[1] for k, v in VOICES.items()}, "styles": STYLES,
            "pexels": bool(os.getenv("PEXELS_KEY", "").strip()), "minutes": MINUTES}


def _env(k):
    import os
    return os.getenv(k, "").strip()


# ---------------- 1) الخطة والنص ----------------
async def _text(body: dict) -> dict:
    """Gemini بالطبقة المجانية، مع بدائل إن أُوقف نموذج."""
    models = [m for m in [_env("STUDIO_TEXT_MODEL"), "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"] if m]
    last = None
    for m in dict.fromkeys(models):
        try:
            return await S._gem(m, body, 180)
        except S.StudioErr as e:
            last = e
            if "غير متاح" in str(e):
                continue
            raise
    raise last or S.StudioErr("لا يوجد نموذج نصوص متاح")


async def plan(p: dict) -> dict:
    n = max(4, round(p["minutes"] * 60 / SEC))
    lang = "العربية (أو اللهجة التي يطلبها المستخدم في طلبه)" if p["lang"] == "ar" else "English (or the dialect the user asks for)"
    vis = ("مقاطع فيديو حقيقية من مكتبة مقاطع جاهزة (Pexels)" if p["visual"] == "stock"
           else f"صور مولّدة بالذكاء الاصطناعي بأسلوب: {p['style'] or 'تختاره أنت بما يناسب الطلب'}")
    prompt = f"""أنت كاتب ومخرج فيديوهات محترف ليوتيوب وتيك توك.
طلب صاحب القناة (نفّذه بدقة، بما فيه أي أسلوب أو نبرة أو لهجة يذكرها):
<<<{p['prompt']}>>>
لغة التعليق الصوتي: {lang}.
مصدر المشاهد: {vis}. الشكل: {'أفقي 16:9' if p['fmt'] == 'wide' else 'عمودي 9:16 (شورتس)'}.
عدد المشاهد: {n} بالضبط. كل مشهد جملة أو جملتان للتعليق الصوتي (12 إلى 22 كلمة، تُقرأ في 6-8 ثوانٍ).
القواعد:
- معلومات صحيحة، ومقدمة تشد الانتباه في أول مشهد، وخاتمة واضحة في الأخير.
- "img": وصف بصري دقيق بالإنجليزية لصورة المشهد (ماذا يظهر، أين، الإضاءة، الزاوية). بلا أي نص أو حروف داخل الصورة. بلا أشخاص حقيقيين مشهورين وبلا شخصيات محمية.
- "q": كلمتان إلى أربع كلمات بالإنجليزية للبحث عن مقطع فيديو حقيقي مناسب للمشهد (مثل: "city night traffic", "ocean waves sunset").
أرجع JSON فقط:
{{"title": "عنوان قصير", "yt_title": "عنوان يوتيوب جذاب (أقل من 90 حرفاً)", "yt_description": "وصف من 3-5 أسطر",
"tags": ["وسم", "..."], "scenes": [{{"say": "...", "img": "...", "q": "..."}}]}}"""
    if S.MOCK:
        return {"title": p["prompt"][:40], "yt_title": p["prompt"][:80], "yt_description": "فيديو تجريبي.", "tags": ["تجربة"],
                "scenes": [{"say": f"المشهد {i + 1}: هذا تعليق تجريبي عن {p['prompt'][:30]} لنختبر الصوت والصورة معاً.",
                            "img": "a calm landscape", "q": "nature"} for i in range(n)]}
    resp = await _text({"contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}})
    txt = "".join(x.get("text", "") for x in S._parts(resp))
    try:
        st = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
    except json.JSONDecodeError:
        raise S.StudioErr("الخطة رجعت بصيغة غير صحيحة. أعد المحاولة.")
    sc = [s for s in st.get("scenes", []) if s.get("say")]
    if len(sc) < 2:
        raise S.StudioErr("الخطة قصيرة جداً. اكتب طلبك بتفصيل أكثر وأعد المحاولة.")
    for s in sc:
        s["img"] = s.get("img") or s["say"]
        s["q"] = (s.get("q") or "").strip() or " ".join(s["img"].split()[:3])
    st["scenes"] = sc[: n + 2]
    return st


# ---------------- 2) الصور (مجاناً) ----------------
class Imgs:
    """Gemini أولاً (إن سمحت الطبقة المجانية)، وعند أول رفض ننتقل لـ Pollinations المجاني لبقية الفيديو."""
    def __init__(self):
        self.gem_off = False
        self.poll_lock = asyncio.Lock()
        self.poll_last = 0.0
        self.used = set()

    async def gemini(self, prompt: str, ar: str) -> bytes | None:
        if self.gem_off or not S._key():
            return None
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ar}}}
        try:
            async with httpx.AsyncClient(timeout=120) as cl:
                r = await cl.post(f"{S.GEM}/{S.IMG_MODEL}:generateContent", json=body, headers={"x-goog-api-key": S._key()})
            if r.status_code == 200:
                return S._inline(r.json())
        except Exception:  # noqa: BLE001
            pass
        self.gem_off = True                      # الطبقة المجانية لا تسمح بالصور الآن: Pollinations لبقية الفيديو
        return None

    async def pollinations(self, prompt: str, W: int, H: int, seed: int) -> bytes:
        tok = _env("POLLINATIONS_TOKEN")
        gap = 3 if tok else 16                   # الحد المجاني بلا حساب: طلب كل 15 ثانية
        url = f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}?width={W}&height={H}&seed={seed}&nologo=true&safe=true"
        last = ""
        for a in range(4):
            async with self.poll_lock:
                wait = self.poll_last + gap - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.poll_last = time.time()
                try:
                    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as cl:
                        r = await cl.get(url, headers={"Authorization": f"Bearer {tok}"} if tok else {})
                except httpx.HTTPError as e:
                    last = str(e)[:120]
                    continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return r.content
            last = f"{r.status_code}"
            await asyncio.sleep(10 * (a + 1))
        raise S.StudioErr(f"خدمة الصور المجانية لا تستجيب الآن ({last}). أعد المحاولة بعد قليل، أو اختر «مقاطع حقيقية».")

    async def make(self, desc: str, style: str, ar: str, W: int, H: int, seed: int) -> bytes:
        if S.MOCK:
            return S._mock_img(desc, ar, seed)
        prompt = (f"{STYLE_EN.get(style, style) or 'high quality'}. {desc}. "
                  f"{'Wide 16:9' if ar == '16:9' else 'Vertical 9:16'} composition. Absolutely no text, letters, captions or watermark.")
        return await self.gemini(prompt, ar) or await self.pollinations(prompt, W, H, seed)


# ---------------- 3) المقاطع الحقيقية (Pexels مجاناً) ----------------
class Stock:
    def __init__(self, fmt: str):
        self.key = _env("PEXELS_KEY")
        self.ori = "landscape" if fmt == "wide" else "portrait"
        self.used = set()
        self.sem = asyncio.Semaphore(3)

    async def _get(self, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(f"https://api.pexels.com/v1/{path}", params=params, headers={"Authorization": self.key})
        if r.status_code == 429:
            raise S.StudioErr("تجاوزت حد Pexels المجاني (200 طلب في الساعة). انتظر قليلاً ثم أعد المحاولة.")
        if r.status_code in (401, 403):
            raise S.StudioErr("مفتاح Pexels غير صحيح. تأكد من PEXELS_KEY في إعدادات المساحة.")
        return r.json() if r.status_code == 200 else {}

    @staticmethod
    def _pick_file(v: dict, short: int):
        fs = [f for f in v.get("video_files", []) if (f.get("file_type") or "").endswith("mp4") and f.get("width") and f.get("height")]
        if not fs:
            return None
        ok = [f for f in fs if min(f["width"], f["height"]) >= short]
        return min(ok, key=lambda f: f["width"] * f["height"]) if ok else max(fs, key=lambda f: f["width"] * f["height"])

    async def _download(self, url: str, path: Path):
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as cl:
            async with cl.stream("GET", url) as r:
                if r.status_code != 200:
                    raise S.StudioErr(f"تعذر تنزيل مقطع ({r.status_code})")
                size = 0
                with open(path, "wb") as f:
                    async for ch in r.aiter_bytes(1 << 16):
                        size += len(ch)
                        if size > 80e6:
                            raise S.StudioErr("مقطع كبير جداً")
                        f.write(ch)

    async def clip(self, q: str, dur: float, short: int, out: Path) -> str:
        """يرجع 'video' أو 'photo' أو '' (لم يجد شيئاً)."""
        async with self.sem:
            for query in dict.fromkeys([q, " ".join(q.split()[:2]), q.split()[0] if q.split() else q]):
                d = await self._get("videos/search", {"query": query, "orientation": self.ori, "per_page": 15, "size": "medium"})
                vids = [v for v in d.get("videos", []) if v.get("id") not in self.used and (v.get("duration") or 0) >= 3]
                vids.sort(key=lambda v: (v.get("duration", 0) < dur, random.random()))
                for v in vids[:3]:
                    f = self._pick_file(v, short)
                    if not f:
                        continue
                    try:
                        await self._download(f["link"], out.with_suffix(".mp4"))
                        self.used.add(v["id"])
                        return "video"
                    except S.StudioErr:
                        continue
            d = await self._get("search", {"query": q, "orientation": self.ori, "per_page": 10})
            for ph in d.get("photos", []):
                if ph.get("id") in self.used:
                    continue
                src = (ph.get("src") or {}).get("large2x") or (ph.get("src") or {}).get("original")
                if src:
                    await self._download(src, out.with_suffix(".jpg"))
                    self.used.add(ph["id"])
                    return "photo"
        return ""


async def _mock_clip(ff: str, out: Path, seed: int):
    c = "0x%06x" % random.Random(seed).randint(0, 0xFFFFFF)
    await S._run(ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
                 "-f", "lavfi", "-i", f"color=c={c}:size=640x360:duration=4", "-filter_complex", "[0:v][1:v]blend=all_opacity=0.4",
                 "-c:v", "libx264", "-preset", "ultrafast", str(out))


# ---------------- 4) الصوت (مجاناً) ----------------
async def speak(text: str, voice: str, lang: str, path: Path, ff: str) -> float:
    if S.MOCK:
        return await S.speak(text, "Kore", lang, path)
    try:
        import edge_tts
        mp3 = path.with_suffix(".mp3")
        await edge_tts.Communicate(text, voice).save(str(mp3))
        await S._run(ff, "-y", "-loglevel", "error", "-i", str(mp3), "-ac", "1", "-ar", "24000", str(path))
        mp3.unlink(missing_ok=True)
        import wave
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001  البديل: صوت Gemini
        if not S._key():
            raise S.StudioErr("خدمة الصوت المجانية لا تستجيب الآن. أعد المحاولة بعد قليل.")
        return await S.speak(text, "Kore" if lang == "ar" else "Puck", lang, path)


# ---------------- 5) التركيب ----------------
def caption_card(text: str, W: int, H: int, rtl: bool, path: Path):
    """ترجمة مكتوبة على الشاشة أسفل الفيديو (شكل الشورتس الشائع)."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if text:
        size = int(min(W, H) * (0.072 if W < H else 0.054))
        for _ in range(4):
            f = S._font(size)
            lines = S._wrap(text, f, W * 0.86, rtl)
            if len(lines) <= 3:
                break
            size = int(size * 0.86)
        lines = lines[:4]
        bh = int(size * 1.25 * len(lines) + size * 0.8)
        y0 = H - bh - int(H * (0.16 if W < H else 0.05))
        g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(g).rectangle([0, y0 - size // 2, W, y0 + bh + size // 2], fill=(0, 0, 0, 120))
        im = Image.alpha_composite(im, g.filter(ImageFilter.GaussianBlur(size // 2)))
        S._text_block(ImageDraw.Draw(im), lines, f, W // 2, y0 + int(size * 0.3), rtl, (255, 255, 255),
                      stroke=max(3, size // 12), sfill=(0, 0, 0), gap=1.25)
    im.save(path)


async def render_clip(ff, src: Path, card: Path, wav: Path | None, dur: float, W, H, out: Path):
    """مقطع فيديو حقيقي بنفس إعدادات ترميز مشاهد الصور (حتى يتصل الفيلم بلا إعادة ترميز)."""
    flt = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={S.FPS},setsar=1,format=yuv420p[bg];"
           f"[1:v]format=rgba,fade=t=in:st=0.3:d=0.4:alpha=1[c];[bg][c]overlay=0:0:shortest=1,format=yuv420p[v]")
    args = [ff, "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(src), "-loop", "1", "-t", f"{dur:.2f}", "-i", str(card)]
    args += ["-i", str(wav)] if wav else ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=24000:cl=mono"]
    args += ["-filter_complex", flt + f";[2:a]apad,atrim=0:{dur:.2f},aresample=44100[a]", "-map", "[v]", "-map", "[a]",
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", str(S.FPS),
             "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(out)]
    await S._run(*args)


async def make_video(job: dict):
    p, jd = job["p"], S.ROOT / job["id"]
    jd.mkdir(parents=True, exist_ok=True)
    rtl = p["lang"] == "ar"
    W, H = (1280, 720) if p["fmt"] == "wide" else (720, 1280)
    ar = "16:9" if p["fmt"] == "wide" else "9:16"
    ff = S._ffmpeg()

    S._step(job, "كتابة الفكرة والتعليق", 5)
    st = await plan(p)
    job["title"] = st.get("title", "")
    sc = st["scenes"]
    (jd / "story.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))

    stock = Stock(p["fmt"]) if p["visual"] == "stock" else None
    if stock and not stock.key and not S.MOCK:
        job["note"] = "لا يوجد مفتاح Pexels، فاستُخدمت صور بالذكاء الاصطناعي بدل المقاطع الحقيقية."
        stock = None
    imgs = Imgs()
    done, total = [0], len(sc) * 2

    def tick():
        done[0] += 1
        S._step(job, f"تجهيز المشاهد والأصوات ({done[0]}/{total})", 10 + 60 * done[0] / total)

    vsem = asyncio.Semaphore(3)

    async def voice(i, s):
        async with vsem:
            s["dur"] = await speak(s["say"], p["voice"], p["lang"], jd / f"s{i:02}.wav", ff)
        tick()

    async def visual(i, s):
        base = jd / f"s{i:02}"
        s["kind"] = "image"
        if stock or (S.MOCK and p["visual"] == "stock"):
            if S.MOCK:
                await _mock_clip(ff, base.with_suffix(".mp4"), i)
                s["kind"] = "video"
            else:
                got = await stock.clip(s["q"], 7.0, min(W, H), base)
                if got == "video":
                    s["kind"] = "video"
                elif got == "photo":
                    Image.open(base.with_suffix(".jpg")).convert("RGB").save(base.with_suffix(".png"))
                else:
                    base.with_suffix(".png").write_bytes(await imgs.make(s["img"], p["style"], ar, W, H, i + 7))
        else:
            base.with_suffix(".png").write_bytes(await imgs.make(s["img"], p["style"], ar, W, H, i + 7))
        tick()

    await asyncio.gather(*(voice(i, s) for i, s in enumerate(sc)), *(visual(i, s) for i, s in enumerate(sc)))
    if imgs.gem_off and not S.MOCK and any(s["kind"] == "image" for s in sc):
        job["note"] = (job.get("note", "") + " الصور من Pollinations المجاني (Gemini المجاني لا يرسم الآن).").strip()

    async def seg(i, s, card, wav, dur, out):
        src = jd / f"s{i:02}.mp4"
        if s["kind"] == "video":
            await render_clip(ff, src, card, wav, dur, W, H, out)
        else:
            await S.render_scene(ff, jd / f"s{i:02}.png", card, wav, dur, W, H, i, out)

    parts, t, srt = [], 0.0, []
    if p.get("intro", True):
        S.title_card(st.get("title") or p["prompt"][:60], W, H, rtl, jd / "t_intro.png", p.get("channel", ""))
        await seg(0, sc[0], jd / "t_intro.png", None, 3.0, jd / "intro.mp4")
        parts.append(jd / "intro.mp4")
        t = 3.0
    for i, s in enumerate(sc):
        S._step(job, f"تركيب الفيديو ({i + 1}/{len(sc)})", 72 + 22 * i / len(sc))
        dur = s["dur"] + 0.6
        caption_card(s["say"] if p.get("subs", True) else "", W, H, rtl, jd / f"c{i:02}.png")
        out = jd / f"v{i:02}.mp4"
        await seg(i, s, jd / f"c{i:02}.png", jd / f"s{i:02}.wav", dur, out)
        parts.append(out)
        srt.append(f"{i + 1}\n{S._srt_time(t + 0.1)} --> {S._srt_time(t + s['dur'] + 0.2)}\n{s['say']}\n")
        t += dur
    (jd / "list.txt").write_text("".join(f"file '{x.name}'\n" for x in parts))
    await S._run(ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "raw.mp4", cwd=str(jd))

    S._step(job, "الموسيقى واللمسات الأخيرة", 96)
    final = jd / "episode.mp4"
    if p.get("music", True):
        S.music(t, jd / "music.wav", hash(job["id"]) % 1000)
        await S._run(ff, "-y", "-loglevel", "error", "-i", "raw.mp4", "-i", "music.wav", "-filter_complex",
                     "[1:a]volume=0.13,aresample=44100,afade=t=out:st=%.2f:d=2[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]" % max(0, t - 2),
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                     final.name, cwd=str(jd))
    else:
        await S._run(ff, "-y", "-loglevel", "error", "-i", "raw.mp4", "-c", "copy", "-movflags", "+faststart", final.name, cwd=str(jd))
    (jd / "episode.srt").write_text("\n".join(srt), encoding="utf-8")

    cover = jd / "s00.png"
    if sc[0]["kind"] == "video":
        await S._run(ff, "-y", "-loglevel", "error", "-ss", "1", "-i", str(jd / "s00.mp4"), "-frames:v", "1", str(cover))
    S.thumbnail(cover, st.get("title") or p["prompt"][:60], rtl, jd / "thumb.jpg")

    desc = st.get("yt_description") or ""
    if any(s["kind"] == "video" for s in sc) and not S.MOCK:
        desc += "\n\nمقاطع الفيديو من Pexels (pexels.com)" if rtl else "\n\nStock footage: Pexels (pexels.com)"
    job["yt"] = {"yt_title": st.get("yt_title"), "yt_description": desc, "tags": st.get("tags")}
    for x in list(jd.glob("v*.mp4")) + [jd / "raw.mp4", jd / "intro.mp4"]:
        x.unlink(missing_ok=True)
    job["secs"] = round(t)
    job["mb"] = round(final.stat().st_size / 1e6, 1)


# ---------------- الواجهة البرمجية ----------------
@router.post("/api/studio/free")
async def new_free(body: dict, _: dict = Depends(S.require_owner)):
    S._ensure_worker()
    prompt = str(body.get("prompt", "")).strip()[:2000]
    if len(prompt) < 3:
        raise HTTPException(400, {"message": "اكتب ماذا تريد في الفيديو"})
    try:
        mins = float(body.get("minutes") or 1)
    except (TypeError, ValueError):
        mins = 1
    lang = "en" if body.get("lang") == "en" else "ar"
    voice = body.get("voice") if body.get("voice") in VOICES else ("ar-SA-HamedNeural" if lang == "ar" else "en-US-GuyNeural")
    p = {"kind": "free", "prompt": prompt, "topic": prompt[:120], "visual": "stock" if body.get("visual") == "stock" else "ai",
         "style": str(body.get("style", "")).strip()[:200], "minutes": min(MINUTES, key=lambda m: abs(m - mins)),
         "fmt": "tall" if body.get("fmt") == "tall" else "wide", "lang": lang, "voice": voice,
         "music": bool(body.get("music", True)), "subs": bool(body.get("subs", True)), "intro": bool(body.get("intro", True)),
         "tg": bool(body.get("tg", True)), "channel": str(body.get("channel", "")).strip()[:60]}
    jid = time.strftime("%m%d%H%M") + "f" + __import__("uuid").uuid4().hex[:4]
    S.JOBS[jid] = {"id": jid, "p": p, "status": "queued", "step": "في الانتظار", "pct": 0, "created": int(time.time())}
    S._save_jobs()
    await S.QUEUE.put(jid)
    return S._pub(S.JOBS[jid])
