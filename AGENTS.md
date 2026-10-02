# AGENTS.md — AndroidAgentPro

وكيل أندرويد ذاتي. الاتصال بالهاتف يتم عبر جسر HTTP على `127.0.0.1:8070`.

## البيئة (مهم)

هذا الجهاز يعمل كـroot على أندرويد (Termux/proot). كل استدعاء bash في opencode
يبدأ shell جديداً نظيفاً: **لا متغيرات بيئة، ولا `.bashrc`**. لذلك يجب تحميل
ملف البيئة في بداية أي أمر يلمس الجسر أو الـLLM:

```bash
set -a; source ~/.agentpro/agentpro_env.sh; set +a
```

هذا الملف يحتوي على:
- `ANDROID_AGENT_PRO_TOKEN` — توكن الجسر (مصادقة Bearer)
- `AGENTPRO_LLM_API_KEY` / `AGENTPRO_LLM_BASE_URL` / `AGENTPRO_LLM_MODEL`

## التحقد من الاتصال بالهاتف (خطوة واحدة)

```bash
set -a; source ~/.agentpro/agentpro_env.sh; set +a
python3 python_core/bridge_client.py
```

المتوقع: `BRIDGE_OK` + `accessibility_connected:true`.
النتيجة `UNAUTHORIZED` تعني أن التوكن تغيّر على الجهاز (إعادة تثبيت/مسح بيانات
التطبيق) → اطلب التوكن الجديد من المالك وحدّثه في `~/.agentpro/agentpro_env.sh`.

## تشغيل الوكيل لهدف حقيقي

```bash
set -a; source ~/.agentpro/agentpro_env.sh; set +a
python3 -m agentpro "<الهدف بالعربية أو الإنجليزية>"
```

أو نسخة APEX (استهداف شجرة الواجهة أولاً):

```bash
python3 -m agentpro.apex.run "<الهدف>"
```

## اختبارات دون جهاز

```bash
python3 -m agentpro --self-test          # حلقة v2 على هاتف محاكى
python3 -m unittest discover -s tests -q # كل الاختبارات
```

## ملاحظات

- لا توجد أسرار في Git؛ التوكنات في `~/.agentpro/` فقط.
- راجع `STATE.md` لتاريخ المشروع، و`README.md` لقائمة الأدوات الكاملة.
- بعد إنهاء مهمة على الهاتف، أبلغ المالك بالنتيجة بشكل مختصر.
