"""صنع فيديو واحد (يُشغَّل في GitHub Actions، مجاناً).
الطلب يأتي من نموذج «Run workflow» (المتغير P_JSON) أو من بوت تيليجرام (bot.py)، والفيديو يُرسل لتيليجرام."""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "studio"))
os.environ.setdefault("STUDIO_DIR", str(HERE / "out"))

import freevid as F  # noqa: E402
import studio as S   # noqa: E402

TG_MAX = 48e6        # حد بوتات تيليجرام 50 ميجا

import httpx  # noqa: E402

TEXT_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.0-flash-lite"]


async def _gem_once(model: str, body: dict, tries=2) -> dict:
    """محاولة سريعة لنموذج واحد؛ عند أي فشل نرمي خطأ لننتقل للنموذج التالي."""
    last = ""
    for a in range(tries):
        try:
            async with httpx.AsyncClient(timeout=90) as cl:
                r = await cl.post(f"{S.GEM}/{model}:generateContent", json=body, headers={"x-goog-api-key": S._key()})
            if r.status_code == 200:
                d = r.json()
                S._parts(d)  # يتأكد أن فيه نصاً فعلاً
                return d
            last = f"{r.status_code} {r.text[:150]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # noqa: BLE001
            last = str(e)[:150]
        await asyncio.sleep(4 + a * 4)
    raise S.StudioErr(f"{model}: {last}")


async def _pollinations(body: dict) -> dict:
    """بديل مجاني أخير إن تعطلت كل نماذج Gemini."""
    txt = "".join(p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", []))
    js = (body.get("generationConfig") or {}).get("responseMimeType") == "application/json"
    if js:
        txt += "\n\nReturn ONLY valid JSON, no markdown."
    async with httpx.AsyncClient(timeout=180) as cl:
        r = await cl.post("https://text.pollinations.ai/openai",
                          json={"model": "openai", "messages": [{"role": "user", "content": txt}],
                                **({"response_format": {"type": "json_object"}} if js else {})})
    r.raise_for_status()
    out = r.json()["choices"][0]["message"]["content"]
    return {"candidates": [{"content": {"parts": [{"text": out}]}}]}


async def _text_robust(body: dict) -> dict:
    """يجرب نماذج Gemini المجانية واحداً بعد الآخر عند أي خطأ، ثم بديل Pollinations."""
    errs = []
    if S._key():
        for rnd in range(2):
            for m in dict.fromkeys([os.getenv("STUDIO_TEXT_MODEL", "").strip()] + TEXT_MODELS):
                if not m:
                    continue
                try:
                    return await _gem_once(m, body)
                except Exception as e:  # noqa: BLE001
                    errs.append(str(e)[:120])
            await asyncio.sleep(20)
    try:
        return await _pollinations(body)
    except Exception as e:  # noqa: BLE001
        errs.append(f"pollinations: {str(e)[:120]}")
    raise S.StudioErr("كل نماذج النصوص المجانية مشغولة الآن، أعد الطلب بعد قليل. " + " | ".join(errs[-3:]))


F._text = _text_robust


def params(d: dict) -> dict:
    """نفس قواعد الاستوديو: قيم آمنة وافتراضيات معقولة."""
    voice = d.get("voice") if d.get("voice") in F.VOICES else "ar-SA-HamedNeural"
    lang = F.VOICES[voice][1]
    try:
        mins = float(d.get("minutes") or 1)
    except (TypeError, ValueError):
        mins = 1
    yes = lambda v, dflt=True: dflt if v is None else (v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes"))
    prompt = str(d.get("prompt", "")).strip()[:2000]
    return {"kind": "free", "prompt": prompt, "topic": prompt[:120], "visual": "stock" if d.get("visual") == "stock" else "ai",
            "style": str(d.get("style") or "").strip()[:200], "minutes": min(F.MINUTES, key=lambda m: abs(m - mins)),
            "fmt": "tall" if d.get("fmt") == "tall" else "wide", "lang": lang, "voice": voice,
            "music": yes(d.get("music")), "subs": yes(d.get("subs")), "intro": yes(d.get("intro")),
            "tg": True, "channel": str(d.get("channel") or "").strip()[:60]}


async def fit_telegram(job: dict):
    """إن كان الفيديو أكبر من حد تيليجرام نضغطه ليصل كاملاً."""
    f = S.ROOT / job["id"] / "episode.mp4"
    for k in (0.9, 0.7):                     # محاولة ثانية أقوى إن بقي أكبر من الحد
        if f.stat().st_size <= TG_MAX:
            return
        kbps = max(200, int(TG_MAX * k * 8 / 1000 / max(1, job["secs"])) - 96)
        tmp = f.with_name("small.mp4")
        await S._run(S._ffmpeg(), "-y", "-loglevel", "error", "-i", str(f), "-c:v", "libx264", "-preset", "veryfast",
                     "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.15)}k", "-bufsize", f"{kbps * 2}k",
                     "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(tmp))
        tmp.replace(f)
        job["mb"] = round(f.stat().st_size / 1e6, 1)


async def make(p: dict) -> dict:
    jid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    job = {"id": jid, "p": p, "status": "running", "step": "", "pct": 0, "created": int(time.time())}
    S.JOBS[jid] = job
    t0 = time.time()
    await F.make_video(job)
    await fit_telegram(job)
    job["took"] = round((time.time() - t0) / 60, 1)
    job["tg"] = await S.to_telegram(job)
    return job


async def main():
    d = json.loads(os.getenv("P_JSON") or "{}")
    p = params(d)
    if len(p["prompt"]) < 3:
        sys.exit("اكتب ماذا تريد في الفيديو")
    job = await make(p)
    print(f"✅ {job.get('title')} · {job['secs']} ث · {job['mb']} م.ب · {job['took']} د · {job['tg']}")
    if job.get("note"):
        print("ℹ️", job["note"])


if __name__ == "__main__":
    asyncio.run(main())
