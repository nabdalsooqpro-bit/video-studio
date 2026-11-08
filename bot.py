"""بوت تيليجرام للاستوديو: تكتب للبوت ما تريد، فيصلك الفيديو جاهزاً في المحادثة.
يعمل كل بضع دقائق في GitHub Actions: يقرأ الرسائل الجديدة، يصنع الفيديوهات واحداً بعد الآخر، ثم ينتهي.
البوت خاص: لا يرد إلا على صاحب القناة (CHAT_ID)."""
import asyncio
import json
import os
import re
import sys

import httpx

import run as R

F, S = R.F, R.S
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OWNER = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = """🎬 أهلاً! أنا استوديو الفيديو الخاص بك.

اكتب لي ما تريد، وأرسل لك الفيديو جاهزاً هنا. أمثلة:
• فيديو تحفيزي عن الصبر بلهجة سعودية، شورتس، دقيقة
• حقائق مذهلة عن الفضاء، مقاطع حقيقية، 3 دقائق
• قصة أطفال كرتون عن أرنب يتعلم الصدق، صوت مصري
• Top 5 travel destinations, English, cinematic

تستطيع أن تذكر في رسالتك:
— المدة: 30 ثانية، دقيقة، 3، 5، 10 دقائق
— الشكل: شورتس/تيك توك (عمودي) أو يوتيوب (أفقي)
— المشاهد: «مقاطع حقيقية» (فيديو متحرك فعلاً) أو صور بأي أسلوب (كرتون، أنمي، واقعي…)
— الصوت: سعودي، مصري، إماراتي، رجل أو امرأة، أو English
— «بدون ترجمة» أو «بدون موسيقى» إن أردت

⏱ أبدأ خلال دقائق من رسالتك (أفحص الرسائل كل 5 دقائق تقريباً)، والدقيقة تأخذ عادة 3-8 دقائق صنع."""


async def tg(method: str, **kw):
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post(f"{API}/{method}", json=kw)
    return r.json() if r.status_code == 200 else {}


async def say(chat, text):
    await tg("sendMessage", chat_id=chat, text=text[:4000])


def guess(text: str) -> dict:
    """تقدير بسيط بلا ذكاء اصطناعي (احتياط إن تعذّر Gemini)."""
    t = text.lower()
    d = {"prompt": text}
    if re.search(r"شورت|تيك ?توك|ريلز|عمودي|short|tiktok|reel|vertical", t):
        d["fmt"] = "tall"
    if re.search(r"حقيقي|stock|real footage|مقاطع", t):
        d["visual"] = "stock"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:د|دقيق|min)", t)
    d["minutes"] = 0.5 if re.search(r"30\s*ث|نصف دقيقة|30 ?s", t) else (float(m.group(1)) if m else 1)
    female = re.search(r"امرأة|بنت|أنثى|صوت نسائي|female|woman", t)
    if re.search(r"english|انجليزي|إنجليزي", t):
        d["voice"] = "en-US-JennyNeural" if female else "en-US-GuyNeural"
    elif "مصر" in t:
        d["voice"] = "ar-EG-SalmaNeural" if female else "ar-EG-ShakirNeural"
    elif "امارات" in t or "إمارات" in t:
        d["voice"] = "ar-AE-FatimaNeural" if female else "ar-AE-HamdanNeural"
    else:
        d["voice"] = "ar-SA-ZariyahNeural" if female else "ar-SA-HamedNeural"
    d["subs"] = not re.search(r"بدون ترجم|بلا ترجم|no subtitle|no caption", t)
    d["music"] = not re.search(r"بدون موسيق|بلا موسيق|no music", t)
    return d


async def understand(text: str) -> dict:
    """Gemini يفهم الرسالة ويستخرج الخيارات، والبقية افتراضية."""
    base = guess(text)
    if not S._key():
        return base
    voices = ", ".join(f"{k} ({v[0]})" for k, v in F.VOICES.items())
    prompt = f"""استخرج إعدادات فيديو من طلب المستخدم التالي، وأرجع JSON فقط.
الطلب: <<<{text}>>>
الحقول:
"prompt": وصف ما يريده في الفيديو (أعد صياغة الطلب بلا الإعدادات التقنية، مع الإبقاء على الموضوع والأسلوب والنبرة واللهجة).
"visual": "stock" إن طلب مقاطع/فيديو حقيقي، وإلا "ai".
"style": أسلوب الصور إن ذكره (مثل كرتون، أنمي، واقعي سينمائي)، وإلا "".
"minutes": واحد من 0.5, 1, 3, 5, 10 (الافتراضي 1).
"fmt": "tall" لشورتس/تيك توك/ريلز/عمودي، وإلا "wide".
"voice": واحد من: {voices}. اختر حسب اللغة واللهجة والجنس المطلوب (الافتراضي ar-SA-HamedNeural، وللإنجليزية en-US-GuyNeural).
"subs": false إن طلب بدون ترجمة، وإلا true. "music": false إن طلب بدون موسيقى، وإلا true."""
    try:
        resp = await F._text({"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}})
        d = json.loads(re.sub(r"^```(json)?|```$", "", "".join(x.get("text", "") for x in S._parts(resp)).strip()))
        return {**base, **{k: v for k, v in d.items() if v not in (None, "")}}
    except Exception:  # noqa: BLE001
        return base


def summary(p: dict) -> str:
    return (f"🎬 بدأت صنع الفيديو:\n«{p['prompt'][:200]}»\n\n"
            f"• {'🎥 مقاطع حقيقية' if p['visual'] == 'stock' else '🎨 صور ' + (p['style'] or 'بالذكاء الاصطناعي')}\n"
            f"• {'30 ثانية' if p['minutes'] < 1 else str(int(p['minutes']) if p['minutes'] == int(p['minutes']) else p['minutes']) + ' د'}"
            f" · {'شورتس عمودي' if p['fmt'] == 'tall' else 'يوتيوب أفقي'}\n"
            f"• الصوت: {F.VOICES[p['voice']][0]}{'' if p['subs'] else ' · بدون ترجمة'}{'' if p['music'] else ' · بدون موسيقى'}\n\n"
            f"سأرسله لك هنا فور جاهزيته ⏳")


async def main():
    if not TOKEN:
        sys.exit("أضف BOT_TOKEN في أسرار المستودع")
    ups = (await tg("getUpdates", timeout=0)).get("result") or []
    if not ups:
        print("لا رسائل جديدة")
        return
    await tg("getUpdates", offset=ups[-1]["update_id"] + 1, timeout=0)       # تأكيد الاستلام حتى لا تُعالج مرتين
    for u in ups:
        m = u.get("message") or {}
        chat, text = (m.get("chat") or {}).get("id"), (m.get("text") or "").strip()
        if not chat or not text:
            continue
        if not OWNER:
            await say(chat, f"رقم محادثتك هو:\n{chat}\n\nأضفه في أسرار المستودع على GitHub باسم CHAT_ID، ثم أرسل لي طلبك.")
            continue
        if str(chat) != OWNER:
            await say(chat, "هذا البوت خاص.")
            continue
        if text.startswith("/") or len(text) < 4:
            await say(chat, HELP)
            continue
        p = R.params(await understand(text))
        await say(chat, summary(p))
        try:
            job = await R.make(p)
            extra = f"\nℹ️ {job['note']}" if job.get("note") else ""
            await say(chat, f"✅ جاهز: {job['secs']} ثانية · {job['mb']} م.ب · صُنع في {job['took']} د{extra}")
        except S.StudioErr as e:
            await say(chat, f"❌ تعذّر صنع الفيديو:\n{e}\n\nأعد إرسال طلبك بعد قليل.")
        except Exception as e:  # noqa: BLE001
            await say(chat, f"❌ خطأ غير متوقع: {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
