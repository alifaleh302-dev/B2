# 🎯 Webook Sniper Bot — v3.0

بوت تيليجرام تفاعلي لحجز تذاكر الفعاليات على **webook.com** تلقائياً. يعمل على Render Free.

---

## ✨ المميزات

| المجال | الوصف |
|---|---|
| 🧭 **واجهة بالأزرار** | قائمة تفاعلية كاملة عبر InlineKeyboard — لا أوامر معقدة |
| 🔐 **تسجيل دخول مرة واحدة** | Playwright يستخدم فقط عند login، ثم JWT Bearer يُحفظ ويُجدَّد تلقائياً |
| 🧩 **حل reCAPTCHA بالتفاعل** | يرسل لك البوت صورة الـ captcha على تيليجرام، وترد بـ `ok` بعد حلّها |
| 👥 **توزيع ذكي** | خوارزمية تقسّم عدد التذاكر على حسابات متعددة احتراماً لـ `max_per_order` |
| 📡 **API-first** | جميع عمليات الحجز تتم عبر `api.webook.com` مباشرةً (aiohttp)، سرعة × 20 |
| 🪑 **SeatCloud / seats.io** | دعم فعلي للأحداث المقعدية عبر holdToken + تحديد مقاعد متجاورة + fallback stalker mode |
| 🔥 **قنّاص سباق الثواني** | حلقة سريعة + prewarming للمقاعد قبل الافتتاح |
| 🔔 **مراقبة خلفية** | يجلب كل 5 دقائق الفعاليات الجديدة وينبّه على كلمات متابعة تختارها |
| 💤 **Keep-Alive** | ping داخلي + endpoint `/health` يمنع نوم Render المجاني |
| 🗃️ **SQLite مستمرة** | المهام والحجوزات والحسابات تبقى بعد إعادة التشغيل |
| 🖥️ **لوحة ويب** | صفحة `/` فيها إحصائيات حيّة |

---

## 🧭 تدفق الاستخدام

1. أرسل للبوت `/start` → قائمة رئيسية
2. **إدارة الحسابات** → ➕ إضافة حساب → تعطي الإيميل → تعطي كلمة المرور
3. اضغط **🔐 تسجيل الدخول** — إذا ظهر reCAPTCHA:
   - البوت يرسل لك صورة
   - تفتحها، تحل الـ captcha (نقرة على "أنا لست روبوتاً" ثم verify)
   - ترسل رسالة نصية للبوت: `ok`
   - البوت يكمل تلقائياً ويستخرج التوكن
4. **الفعاليات الجارية** → اختر فعالية → اختر نوع التذكرة → اختر العدد → **تأكيد**
5. البوت يحجز على جميع الحسابات بالتوازي ويُرجع روابط PayTabs جاهزة

---

## 🏗️ البنية

```
app/
├── core/
│   ├── config.py           ← إعدادات من env
│   ├── logging_setup.py    ← logger موحد
│   └── storage.py          ← SQLite
├── services/
│   ├── webook_api.py       ← aiohttp client لـ api.webook.com
│   ├── event_discovery.py  ← استكشاف فعاليات من الصفحة الرئيسية
│   ├── auth_service.py     ← Playwright login + JWT refresh
│   ├── captcha_broker.py   ← ربط Playwright بحوار Telegram
│   ├── distributor.py      ← توزيع التذاكر
│   ├── booking_orchestrator.py ← حجز متوازي
│   ├── event_monitor.py    ← حلقات المراقبة الخلفية
│   └── keep_alive.py       ← منع النوم
└── bot/
    ├── notifier.py         ← Telegram Bot API wrapper
    ├── keyboards.py        ← جميع InlineKeyboards
    ├── state.py            ← FSM للمحادثات متعددة الخطوات
    └── handlers.py         ← dispatcher + جميع الـ handlers
main.py                     ← FastAPI + lifespan + webhook
```

---

## 🚀 النشر على Render

1. ادخل إلى [Render Dashboard](https://dashboard.render.com)
2. Render سيلتقط `render.yaml` — Docker، خطة Free، منطقة Frankfurt
3. عيّن متغيرات البيئة السرية:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. اضغط Deploy — البوت سيرد على `/start` خلال دقائق

**نصيحة:** أضف `https://your-app.onrender.com/ping` إلى [UptimeRobot](https://uptimerobot.com) مجاناً لضمان عدم النوم مطلقاً.

---

## ⚙️ متغيرات البيئة

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | ❗ مطلوب |
| `TELEGRAM_CHAT_ID` | — | ❗ مطلوب |
| `AUTHORIZED_CHAT_IDS` | = CHAT_ID | معرفات مصرح لها إضافية (فواصل) |
| `WEBOOK_LANG` | `en` | لغة API (en\|ar) |
| `WEBOOK_PUBLIC_TOKEN` | ثابت | توكن العامة للـ webook |
| `EVENT_POLL_INTERVAL` | `300` | ث بين فحوص اكتشاف الفعاليات |
| `SNIPER_POLL_INTERVAL` | `2` | ث بين تكات القنّاص |
| `KEEP_ALIVE_INTERVAL` | `600` | ث بين ping خارجي |
| `LOGIN_CAPTCHA_TIMEOUT` | `180` | ث انتظار حل الـ captcha من المستخدم |
| `SEATSIO_PREWARM_ENABLED` | `true` | تسخين خريطة المقاعد وحالاتها مسبقاً |
| `SEATSIO_STALKER_ENABLED` | `true` | محاولة التقاط المقاعد المحررة سريعاً |
| `TARGET_BLOCKS` | — | بلوكات مفضلة مثل `S1,S2,VIP` |
| `CAPTCHA_API_KEY` | — | مفتاح 2captcha اختياري لتسجيل دخول آلي أقوى |
| `PROXY_SERVER` | — | بروكسي مدفوع اختياري لزيادة الثبات |
| `HEADLESS` | `true` | Chromium headless |
| `LOG_LEVEL` | `INFO` | `DEBUG` لتفاصيل أكثر |

---

## ⚠️ قيود وتحذيرات

- **webook يستخدم reCAPTCHA** — لا يمكن تجاوزه برمجياً. نحن نحوّله إليك لحلّه يدوياً مرة واحدة لكل login.
- **طريقة الدفع PayTabs فقط** — webook لا يدعم PayPal. البوت يُرجع رابط PayTabs مباشرة وأنت تختار طريقة الدفع من صفحة PayTabs.
- **الخطة المجانية من Render = 512MB RAM** — Playwright يُشغَّل لحظياً فقط عند login (حتى 60 ثانية)، ثم يُغلق. بقية العمليات HTTP خفيفة جداً.
- هذا المشروع تعليمي. استخدام بوتات الحجز قد يخالف شروط الخدمة.
