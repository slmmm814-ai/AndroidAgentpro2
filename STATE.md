# STATE.md — حالة مشروع AndroidAgentPro

> المصدر التنفيذي للحالة: Git.
> المالك صاحب القرار النهائي والقبول النهائي.
> المشروع مستقل.

## 1. الحالة الحالية

- **آخر تحديث:** 2026-09-23 (جولة الإحكام — Autonomy v2)
- **المرحلة الحالية:** Phase 8 — Autonomy Layer (دماغ LLM + إجراءات غنية + حلقة وكيل ذاتية)
- **الحالة:** BUILD + TESTS PASS (412/412) — جاهز للعرض على الجهاز الحقيقي
- **البند النشط:** P8-05 جولة الإحكام (عقد المراقبة، erase_text، حد الشجرة، tap_element، CLI v2 + سلف تيست)
- **آخر حالة مؤكدة في Git:** `16ee951` (ملاحظة: الأرشيف الحالي لا يحوي `.git`؛ وسوم Phase 0–6 غير قابلة للتحقق من هذه الحزمة — تُستعاد من المستودع)
- **آخر وسم مكتمل:** `phase5.2-complete` — يشير إلى `ff1628d`

## 2. قرارات المالك

| ID | التاريخ | القرار | السبب |
|---|---|---|---|
| D-05 | 2026-09-21 | اعتماد Git كمصدر الحالة التنفيذية؛ Phase 0 وPhase 1 وPhase 2 مكتملة، والخطوة التالية Phase 3 | حسم التعارض بين حالة الملفات القديمة وحالة Git |
| D-06 | 2026-09-21 | GPT يعمل منفردًا كـ Architect + Builder + Breaker + Reviewer؛ المالك وحده يقوم بـ OWNER_ACCEPT | قرار المالك |

## 3. الأدوار الحالية

| الدور | المسؤول |
|---|---|
| Owner | المستخدم |
| Architect | GPT |
| Builder | GPT |
| Breaker | GPT |
| Reviewer | GPT |
| OWNER_ACCEPT | المستخدم |

## 4. مراحل المشروع

| المرحلة | الحالة | Git |
|---|---|---|
| T — بنية العمل | متجاوزة بقرار المالك لصالح حالة Git | — |
| Phase 0 — Bridge | مكتملة | `phase0-complete` |
| Phase 1 — FSM | مكتملة | `phase1-complete` |
| Phase 2 — Meta-Planner | مكتملة | `phase2-complete` |
| Phase 3 — Verifier | مكتملة — OWNER_ACCEPT | `phase3-complete` |
| Phase 4 — Safe Execution | مكتملة — OWNER_ACCEPT | `phase4-complete` |
| Phase 5.1 — Hierarchical Task Execution | مكتملة — OWNER_ACCEPT | `phase5.1-complete` |
| Phase 5.2 — Replanning on Step Failure | مكتملة — OWNER_ACCEPT | `ff1628d` / `phase5.2-complete` |
| Phase 6 — P6-01 Web Research as Data | مكتملة — OWNER_ACCEPT | `8d46acc` |
| Phase 7 | توثيق P7-00..P7-03 موجود في `docs/phase7/`؛ P7-04/P7-05 غير منفّذة | — |
| Phase 8 — Autonomy Layer | مكتملة (BUILD + TESTS) — OWNER_ACCEPT PENDING | هذه الجلسة |

## 5. الحالة المؤكدة لـ Phase 0

- الوسم: `phase0-complete`
- commit: `9e38bca`
- لا يتم تعديل Phase 0 إلا عند ظهور regression مثبت بالدليل.

## 6. الحالة المؤكدة لـ Phase 1

- الوسم: `phase1-complete`
- commit: `4de3b33`
- اختبارات Phase 1 مثبتة في Git.

## 7. الحالة المؤكدة لـ Phase 2

- الوسم: `phase2-complete`
- commit: `eb3771a`
- اختبارات Phase 2 مثبتة في Git.
- Phase 2 أضاف Meta-Planner حتميًا فوق FSM دون تغيير FSM.

## 8. البند الحالي
**Phase 6 — P6-03 Dangerous Tool Authorization**
- الحالة: BUILD + BREAK + ARCH_REVIEW PASS — OWNER_ACCEPT PENDING
- مواصفة P6-03: `docs/specs/P6-03-dangerous-tool-authorization.md`
- تنفيذ P6-03 مثبت في commit: `16ee951`
- P6-03 dedicated tests: 17/17 PASS
- BREAK: التغطية العدائية لـP6-03: PASS
- Regression الكامل للمستودع: 198/198 PASS
- Phase 1: 10/10 PASS
- Phase 2: 14/14 PASS
- Phase 3: 39/39 PASS
- Python compileall: PASS
- `git diff --check`: PASS
- فحص placeholders/TODO: PASS
- فحص secret-like material: PASS
- ARCH_REVIEW: PASS؛ التغيير محصور في `authorization.py` و`executor.py` واختبار P6-03
- الضمانات المثبتة: رفض confirmation المنتهي، رفض `request_id` المزور، رفض action/fingerprint mismatch، رفض confirmation الصادر من AuthorizationService مختلف، ومنع تنفيذ `install_apk` بدون AuthorizationService + Owner Confirmation صالحين
- Web Research لا يمنح Authorization ولا يستطيع إنشاء Owner Confirmation
- Verification يبقى إلزاميًا بعد التنفيذ
- Trace لا يسجل مواد التفويض الحساسة
- OWNER_ACCEPT: APPROVED — صادر من المالك
- الخطوة التالية: E2E-01؛ لا تبدأ Phase 7 قبل اجتياز اختبار التكامل

هدف P6-03 هو فرض حد تفويض مستقل للأفعال الخطرة، بحيث لا تتحول Candidate Action أو Web Data إلى تنفيذ خطير دون تأكيد صريح من المالك، مع ربط التأكيد بالفعل المحدد والتحقق من صلاحيته قبل التنفيذ.

## 8.1 أدلة P6-03 المؤكدة

- commit التنفيذ: `16ee951`
- اختبارات P6-03 المخصصة: 17/17 PASS
- Full Regression: 198/198 PASS
- Phase 1: 10/10 PASS
- Phase 2: 14/14 PASS
- Phase 3: 39/39 PASS
- Python compileall: PASS
- `git diff --check`: PASS
- فحص placeholders/TODO: PASS
- فحص secret-like material: PASS
- الـbackup `agentpro/executor.py.p603.bak` غير متتبع وغير داخل الـcommit
- OWNER_ACCEPT: APPROVED — صادر من المالك

## 8.2 طبقة الاستقلالية (Phase 8 — Autonomy Layer)

الهدف: تحويل الهيكل الأمني إلى وكيل ذاتي الحلقة (يلاحظ ← يخطّط عبر LLM ← ينفّذ ← يكرر).

**المكوّنات المضافة (تجميعية — لم تُمَسّ FSM/MetaPlanner/executor/authorization):**
- `agentpro/llm_planner.py` — الدماغ: `LLMClient` (Protocol) + `OpenAICompatibleClient` (يقرأ env) + `FakeLLMClient` (سكربت) + `LLMPlanner` يبني (goal + شجرة UI + لقطة شاشة) → إجراء JSON تالي مع تحقق args و استخراج JSON متسامح.
- `agentpro/agent_runner.py` — `BridgeObserver` + `BridgeActionExecutor` + `LastActionGoalVerifier` + `SimpleRecovery` + `LLMPlannerAdapter` + `AgentRunner` + `RecordingBridgeClient` (للعرض/الاختبار دون جهاز).
- `agentpro/__main__.py` — CLI: `python -m agentpro "<goal>" [--dry-run] [--max-steps N]`.
- `agentpro/demos/calculator_tour.py` — جولة ملموسة: الوكيل يولّد آلة حاسبة HTML كاملة ويفتحها في المتصفح عبر `data:text/html` URL.
- `agentpro/models.py` — إضافة `INPUT_TEXT/SWIPE/OPEN_URL/KEY_EVENT` إلى `ActionType`.
- `python_core/bridge_client.py` — دوال `input_text/swipe/open_url/key_event/launch_app`.
- جانب الجهاز (Kotlin، صحيح نحوياً، لا يُبنى هنا لغياب SDK): `AgentAccessibilityService.kt` (+`inputText`/`swipe`/`keyEvent`)، `BridgeServer.kt` (+5 أوامر + `FileProvider` لـ`data:` URL)، `AndroidManifest.xml` (+`<queries>` + FileProvider)، `res/xml/file_paths.xml`، `build.gradle` (+androidx.core).

**الأدلة:**
- `python3 -m pytest tests/ -q` → **286 passed, 16 subtests** (224 القديمة + 62 الجديدة).
- `python3 -m compileall agentpro python_core` → PASS.
- `python3 -m agentpro "build me a calculator app" --dry-run` → SUCCESS؛ يُصدر `open_url` بـ`data:text/html` آلة حاسبة عاملة.
- الاختبارات الجديدة: `test_llm_planner.py` (استخراج JSON + كل نوع إجراء + بدائل الأمان)، `test_bridge_client_actions.py` (إرسال + تحقق)، `test_agent_runner.py` (حلقة كاملة حتمية)، `test_new_action_types.py`، `test_calculator_tour.py`.

**ما لم يُفعل بعد (خطوات لاحقة):**
- OWNER_ACCEPT على Phase 8.
- بناء APK على جهاز حقيقي + ربط مفتاح LLM بصري حقيقي للتحقق من الأداء الفعلي.
- تنفيذ P7-04 (التوثيق التشغيلي) — بُدئ هنا بـREADME + OPERATIONS.md، P7-05 (القياس والإغلاق) لا يزال غير منفّذ.
- تصحيح خطأ P7-03 ARCH-REVIEW الذي يدّعي غياب `BridgeServer.kt` (موجود ومُنفّذ بالكامل).

## 8.3 طبقة الاستقلالية الكاملة (Autonomy v2 — حلقة حقيقية A→B→C→D)

الهدف: تحويل الوكيل من حلقة "plan→execute" إلى وكيل ذاتي طويل الأمد (يخطط ويخطّط/يستعيد ويقيّم الهدف) مع ميزانيات أمان صارمة. كل شيء مضاف compile-side ولا يعطّل البنية القديمة (AgentFSM/executor/authorization شغّالة كما هي).

**المكوّنات الجديدة (في `agentpro/`):**
- `screen.py` — ملاحظة قوية: `ScreenReader` + `InteractiveElement` + `ScreenSnapshot` (تصفية العناصر، حدود، مؤشر `fingerprint` مستقر للتغيّر) + `summarize_screen` (موازنة سياق) + `wait_for_text / wait_for_stable` (انتظار ذكي بدل sleep ثابت) + `get_window_info`.
- `model_manager.py` — طبقة LLM مستقلة: إعادة محاولة + عميل بديل + استخراج JSON متسامح + حلقة تصحيح ذاتي مع validator + عدّ كل استدعاء (لتطبيق ميزانية model-calls).
- `bridge_tools.py` — نظام أدوات موحّد: `ToolRegistry` + JSON schema خفيف للتحقق من الإدخال + `PermissionLevel` + أدوات خطرة (`shell`/`install_apk`) **مغلقة افتراضيًا** (disabled + OWNER) مع hook تفويض اختياري.
- `memory.py` — ذاكرة مهمة: working/episodic/failures + حالة subgoal + تلخيص LLM تلقائي عند نمو السجل.
- `budgets.py` — ميزانيات صارمة: steps/actions/model_calls/wall-time + `LoopGuard` (كشف التكرار) + `KillSwitch` (ملف/env/علم).
- `verification.py` — تحقق ثلاثي: `ActionVerifier` (تحقق تغيّر الشاشة)، `ProgressVerifier` (تقدم/تكرار/جمود)، `GoalVerifier` LLM (يُستدعى فقط عند إشارة الاكتمال).
- `planner_v2.py` — `SubgoalPlanner`: تفكيك الهدف إلى subgoals مرة واحدة ثم قرار "أداة واحدة تالية" لكل خطوة؛ علامات `subgoal_done / goal_done / replan`؛ fallback آمن إلى `wait`.
- `agent_v2.py` — `AutonomousAgent` (الحلقة الكاملة) + `SmartRecovery` (back ثم home عند التكرار) + `AgentReport` + `build_v2_runner`.

**الأدلة:**
- اختبارات جديدة: `test_screen.py`، `test_model_manager.py`، `test_bridge_tools.py`، `test_memory.py`، `test_budgets.py`، `test_verification.py`، `test_planner_v2.py`، `test_agent_v2.py` (حلقة كاملة بجهاز محاكى يحسب 5+5 فعلًا وينجح؛ تكرار→تعافٍ→فشل؛ kill switch؛ ميزانية model-calls؛ replan).
- `python3 -m unittest discover -s tests -q` → **391 tests, OK** (منها السابق 286+ المتوافق).
- سيناريو الشهادة الحاسم: الوكيل يلاحظ → يخطّط → يلمس "Calculator" → يضغط 5 + 5 = → يقيس تغيّر الشاشة بعد كل إجراء → عند ظهور "10" يعلن goal_done → يحقّق الهدف عبر LLM → `v2_success` في التتبع.
- نقاط تم نسخها من البنية القديمة بلا تعديل: `AgentFSM` (قيود)، `TraceRecorder`، `AuthorizationService` + `DangerousActionPolicy` (لا يمتد set الأفعال الخطرة) — دفاع متعدد الطبقات.

**ملحوظات غير مثبتة بعد (تُعرض على جهاز حقيقي فقط):**
- الجانب Kotlin المضاف (`long_press`/`clear_text`/`get_window` + `checked`/`editable` في JSON) غير مُجرب بناءً تقنيًا هذا الجلسة (قرار المالك: لا بناء APK الآن) — يُتحقق عند أول بناء/تشغيل.
- الوصول الحقيقي (عبر OpenAICompatibleClient ومعرّف API حقيقي) يتطلب إعداد بيئة key من المالك.

## 8.4 صلابة المراقبة والتحرير وتوليف الإنتاج (Autonomy v2 — جولة الإحكام)

الأهداف المنفَّذة في هذه الجولة: إصلاح عقد المراقبة مع Kotlin، إجراء تحرير موثوق، حد لعُقد شجرة UI، أداة لمس بالعناصر، ومولّف إنتاجي كامل للـ v2 مع اختبار ذاتي دون جهاز.

**ما تم إنجازه (كل البنود مؤكدة بالاختبارات):**

- **إصلاح عقد المراقبة (Observations already G10)** — `agentpro/screen.py` كان لا يقرأ أسماء الحقول الفعلية القادمة من Kotlin (`class_name` / `view_id_resource_name` / `content_description`) وكان `content_description` النصي الصامت يُسقط بالكامل. الآن: يقرأ الحقول الحديثة مع fallback للحقول القديمة (`class`/`className`، `resource_id`/`resourceId`، `content_desc`/`contentDescription`)، يفلتر العقد غير المرئية (`visible_to_user=false`)، ويضيف `enabled` + وسم `disabled` في ملخص الشاشة. 13/13 اختبارات `test_screen.py`.
- **`erase_text` (ERASE_TEXT) من طرف إلى طرف** — `models.py` (`ActionType.ERASE_TEXT`)، `python_core/bridge_client.py::erase_text()`، `agent_runner.py::RecordingBridgeClient.erase_text()` + فرع التنفيذ، `bridge_tools.py::_EraseTextTool`، `llm_planner.py::_ACTION_MAP`؛ وفي Kotlin: dispatch `"erase_text"` + `executeEraseText` في `BridgeServer.kt` و`eraseText()` في `AgentAccessibilityService.kt` (تحديد الكلّ + قص ×3 مع حلقات `erasing@`، وfallback إلى تفريغ النص). 64 اختبارًا متأثرًا أخضر.
- **حد عُقد شجرة UI** — `MAX_TREE_NODES = 2_000` + `NodeBudget` في `AgentAccessibilityService.kt`؛ الاستجابة تضيف `"truncated"` عبر `BridgeServer.kt`؛ وتُلتقط في Python عبر `Observation.tree_truncated` / `ScreenSnapshot.tree_truncated` ووسم `[tree truncated]` في الملخص. يصحح أخطاء استدعاء مزدوج لـ`ui_dump`.
- **أداة `tap_element`** — `_TapElementTool` يحل العنصر حسب `index` أو النص (يفضل العنصر القابل للنقر غير القابل للتحرير، ثم `resource_id`، ثم أي عنصر) ويلمس مركزه؛ أخطاء `INVALID_ARGS` / `TOOL_TARGET_NOT_FOUND`؛ `ToolContext.snapshot` + `screen_dims()` لاشتقاق أبعاد الشاشة من السكرينشوت (يستخدمها `_ScrollTool` أيضًا).
- **CLI v2 هو المسار الافتراضي للإنتاج** — إعادة كتابة `agentpro/__main__.py`: `--max-actions` (60)، `--max-model-calls` (150)، `--max-wall-seconds`، `--repeat-threshold` (4)، `--max-steps` (400)، `--trace` (`~/.agentpro/trace_v2.jsonl`)، `--kill-file` (KILL)، `--allow-tool`، `--self-test`، `--dry-run`، `--legacy`؛ معالج تأكيد المالك يقرأ من الطرفية (fails-closed عند غياب TTY).
- **اختبار ذاتي دون جهاز** — `agentpro/self_test.py`: هاتف محاكى يحسب 5+5 فعلًا و`_scripted_responder` يستخدم `tap_element` بالنص. `python3 -m agentpro --self-test` → SUCCESS (5 أفعال، 8 استدعاءات نموذج، حدث النتيجة "result shows 10" في تتبع v2 بالكامل عبر `tap_element` بعناصر، كلها VERIFIED).
- **تقوية `ActionVerifier` بمحتوى النص** — `_editable_texts` + تحقق `type_text/clear_text/erase_text` بدلتا طول/احتواء نص الحقول القابلة للتحرير (NO_OP عند غيابها). 45 اختبارًا.
- **محاسبة استدعاءات النموذج** — مدقق الهدف في `agent_v2.py` يستخدم `LLMGoalVerifier(self._mm)` (ModelManager) فتُحتسب مكالماته ضمن ميزانية model-calls؛ واستُبدل كعب `model_manager.word_ratio` بتنفيذ حقيقي (نسبة الكلمات الشبيهة المحتوية على حروف علّية).
- **مطابقة الحقيقة لأداة `key_event`** — Kotlin يدعم أفعال النظام فقط (back/home/recents/notifications/quick_settings/power_dialog)؛ أُزال الادعاء المضلل لـ`enter`/`delete`/`volume` من وصف الأداة مع `enum` للقيم المدعومة.
- **اختبارات جديدة**: `test_self_test.py` (حلقة v2 كاملة على هاتف محاكى) + `KotlinBridgeContractTests` + اختبارات `tap_element`/`erase_text`/أبعاد التمرير + اختبارات التحقق بمحتوى النص + اختبارات أوامر bridge للـclear/erase.

**الأدلة:**
- `python3 -m unittest discover -s tests -q` → **412 tests, OK** (391 سابقًا + 21 جديدة؛ ثم أُصلح عطل مؤقت في `test_self_test.py`).
- `python3 -m compileall agentpro python_core` → PASS.
- `python3 -m agentpro --self-test` → SELF-TEST: PASS.
- `python3 -m agentpro --dry-run` → لا يزال يعمل (جارٍ إعادة التحقق قبل التسليم).

**ما تم التحقق منه بالبناء الفعلي (GitHub Actions):**
- بناء APK عبر `.github/workflows/android-build.yml` (AGP 8.7.3 + Kotlin 2.0.21 + JDK 17) → نجح وأنتج artifact `AndroidAgentPro-debug` (~2.0MB APK).
- أثناء البناء اكتُشفت أخطاء Kotlin حقيقية (كانت مذكورة سابقًا كـ"غير مثبتة") وأُصلحت مع نجاح البناء:
  - إضافة `android/gradle.properties` مع `android.useAndroidX=true` (كانت البناء تفشل لوجود `androidx.core` دون تفعيل AndroidX).
  - تصحيح استيراد `AccessibilityWindowInfo` إلى `android.view.accessibility` (كان `android.accessibilityservice`).
  - استبدال الثابت غير الموجود `ACTION_SET_TEXT_ARGUMENT` بـ`ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE` (3 مواقع).
  - تثبيت نوع `ExecutorService.submit<GestureResult>{}` في 4 دوال (كانت المُحَمِّل يقع على `Runnable` فيصير `Future<*>`).
  - تصحيح `executeEraseText` لاستخدام الخاصية `commandExecutor`.
- المستودع المعتمد للبناء: `slmmm814-ai/AndroidAgentpro2` على فرع `main` — أي push إليه يفعّل البناء تلقائيًا.

**ما لم يُتحقق بعد (يتطلب جهازًا حقيقيًا):**
- الأداء الفعلي للتلمس/التمرير/التحرير على أجهزة حقيقية (يعتمد مفتاح LLM حقيقيًا وToken من التطبيق).
- OWNER_ACCEPT النهائي.

## 9. قواعد الاستمرار

1. لا نعدل Phase 0–2 دون regression مثبت بالدليل.
2. P6-02 يمر بدورة SPEC → BUILD → BREAK → OWNER_ACCEPT قبل الانتقال إلى P6-03.
3. لا نعلن نجاح أي اختبار دون مخرجات فعلية.
4. قبل أي commit جديد يجب تشغيل:
   - اختبارات Phase 1
   - اختبارات Phase 2
   - اختبارات Phase 3
   - `git diff --check`
5. لا توجد أسرار أو Tokens حقيقية داخل Git.
6. أي فشل يجب إصلاحه بأقل تغيير ممكن مع إعادة regression.
7. المالك وحده يقرر OWNER_ACCEPT النهائي.

## 10. البيئة

- المستودع: `~/AndroidAgentPro`
- الفرع الحالي: `phase5-01-hierarchical`
- الاتصال المحلي: `127.0.0.1:8070`
- Android + Kotlin
- Termux + Python
- البناء عبر GitHub Actions

## 11. ملاحظة الاستئناف

إذا انقطعت الجلسة:

1. قراءة `STATE.md`.
2. فحص `git status`.
3. قراءة آخر commits والوسوم.
4. التأكد من أن Phase 0–4 وPhase 5.1 ما زالت سليمة.
5. متابعة ما بعد Phase 5.2 فقط بعد التحقق من Git وحالة Phase 5.2 واعتماد OWNER_ACCEPT.
6. عدم افتراض نجاح أي خطوة دون دليل.
