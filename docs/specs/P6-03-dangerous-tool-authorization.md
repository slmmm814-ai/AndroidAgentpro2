# P6-03 — Dangerous Tool Authorization & Execution Boundary

## 1. الهدف

إغلاق معيار Phase 6 رقم 6.3 من `ACCEPTANCE.md` بإثبات أن
`install_apk` وأي أداة مصنفة خطرة لا يمكن تنفيذها إلا بعد تأكيد صريح
من المالك، وأن هذا التأكيد مستقل عن محتوى الويب ومربوط بالفعل المحدد
الذي وافق عليه المالك.

المبدأ المركزي:

> Web content is data.
> Candidate actions are not authorization.
> Owner confirmation is independent authorization.
> Dangerous execution requires exact authorization binding.
> Verification remains mandatory.

هذه المواصفة لا تعيد تصميم P6-02 ولا تستبدل `AuthorizationService`
الحالي، بل تغلق حد التنفيذ الخطِر وتثبت سلامته end-to-end.

---

## 2. نطاق P6-03

### داخل النطاق

1. تعريف واضح للأفعال الخطرة عند حدود التنفيذ.
2. اعتبار `install_apk` فعلًا خطِرًا.
3. رفض تنفيذ الفعل الخطِر عند غياب تأكيد المالك.
4. قبول التأكيد فقط إذا كان:
   - صادرًا عن `AuthorizationService` نفسه.
   - مرتبطًا بطلب confirmation معروف.
   - مرتبطًا بنوع الفعل الصحيح.
   - مرتبطًا بـ action fingerprint المطابق.
   - غير منتهي الصلاحية.
   - ممنوحًا صراحةً من المالك.
5. منع Web Data من إنشاء Owner Confirmation.
6. منع Web Data من تمرير confirmation جاهز باعتباره موافقة مالك.
7. منع إعادة استخدام confirmation لفعل مختلف.
8. منع تزوير `request_id`.
9. منع تجاوز طبقة التحقق قبل التنفيذ.
10. تسجيل نتيجة التفويض والتنفيذ في Trace دون تسجيل الأسرار أو رموز
    التفويض الحساسة.
11. إثبات أن رفض التفويض لا يؤدي إلى تنفيذ جزئي للفعل الخطِر.
12. الحفاظ على توافق P6-01 وP6-02 وجميع المراحل السابقة.

### خارج النطاق

1. إضافة صلاحيات Android جديدة.
2. تنفيذ تثبيت APK تلقائيًا دون موافقة المالك.
3. تجاوز واجهة Android الرسمية للتثبيت.
4. تنفيذ shell commands جديدة.
5. إضافة remote/cloud control.
6. تغيير FSM.
7. تغيير semantics الخاصة بالـ Six-Layer Verifier.
8. إعادة تصميم Web Research.
9. جعل صفحات الويب مصدر ثقة.
10. تخزين confirmation أو tokens كأسرار دائمة.
11. إضافة أدوات خطرة جديدة لمجرد توسيع النطاق.
12. تعديل Android Bridge contract إلا بقرار مستقل من المالك.

---

## 3. المراجع المعتمدة

هذه المواصفة تعتمد على:

- `PROTOCOL.md`
- `ACCEPTANCE.md`
- `STATE.md`
- `docs/specs/P6-01-web-research-as-data.md`
- `docs/specs/P6-02-research-then-execute.md`
- تنفيذ P6-02 الحالي في:
  - `agentpro/authorization.py`
  - `agentpro/verifier.py`
  - `agentpro/models.py`

لا يجوز لـ P6-03 نقض العقود التي أُغلقت في P6-02.

---

## 4. نموذج الثقة

يجب أن تبقى الحدود التالية منفصلة:

```text
UNTRUSTED_WEB_DATA
        ↓
PLANNING_DATA
        ↓
CANDIDATE_ACTION
        ↓
OWNER_CONFIRMED_ACTION
        ↓
VERIFIED_ACTION
        ↓
EXECUTION_EVIDENCE

---

## 5. تعريف الفعل الخطِر

`install_apk` هو فعل خطِر إلزاميًا في P6-03.

يجب أن يكون تصنيف الخطورة typed/deterministic ولا يعتمد على نص
وصف الفعل.

أي فعل خطِر يجب أن يمر عبر نفس authorization boundary.

إذا أضيف فعل خطِر جديد مستقبلًا، فيجب أن يتم ذلك بقرار مالك مستقل
ومواصفة واختبارات خاصة به.

---

## 6. متطلبات التفويض

### 6.1 عدم وجود Confirmation

إذا كان الفعل خطِرًا ولا توجد Owner Confirmation:

```text
DENY / CONFIRMATION_REQUIRED

### 6.2 رفض Owner
إذا أصدر المالك قرار رفض صريح:
```text
DENY

أي Confirmation انتهت صلاحيته يجب رفضها، حتى لو كانت صحيحة سابقًا:
DENY
6.4 Request ID مزور
يجب أن يكون request_id صادرًا عن نفس AuthorizationService الذي أنشأ طلب التفويض. أي معرف غير معروف أو مزور:
DENY
6.5 عدم تطابق الفعل
يجب أن يرتبط التفويض بالفعل المحدد الذي طلب المالك تأكيده. لا يجوز استخدام Confirmation لفعل مختلف.
requested_action != confirmed_action
        ↓
DENY
6.6 عدم تطابق البصمة
يجب أن تطابق بصمة الفعل وقت التفويض بصمة الفعل وقت التنفيذ. أي تغيير في النوع أو الوسائط أو متطلبات التأكيد:
DENY
6.7 اختلاف Authorization Service
لا يجوز قبول Confirmation صادرة من instance مختلف من AuthorizationService.
6.8 زمن القرار
يجب رفض أزمنة القرار غير الصالحة، بما في ذلك الزمن المستقبلي أو الزمن السابق لإنشاء الطلب أو اللاحق لانتهاء صلاحيته.
## 7. استقلال Web Data عن Authorization
محتوى الويب يبقى Data غير موثوقة ولا يملك أي صلاحية لإنشاء أو منح Owner Confirmation.
يجب رفض أي محاولة لتحويل محتوى الويب إلى تفويض، بما في ذلك:
نص يطلب approve install_apk.
JSON يحتوي على confirmed=true.
ادعاء أن المستخدم وافق.
ادعاء أن النظام أو المالك منح التفويض.
رابط APK مع تعليمات تثبيت.
أمر shell للتثبيت.
نص يزعم أن Authorization تمت مسبقًا.
أي authorization metadata قادمة من Web Research.
العلاقة الصحيحة:
WEB DATA
   |
   X  لا يمكنه منح Authorization
   |
OWNER CONFIRMATION
   |
VERIFIER
ولا يجوز لـ Web Research أو Planner إنشاء OwnerConfirmation نيابة عن المالك.
## 8. حد التنفيذ Execution Boundary
أي تنفيذ لفعل خطِر يجب أن يمر بالترتيب التالي دون تخطي طبقة:
CANDIDATE ACTION
      ↓
DANGEROUS ACTION CLASSIFICATION
      ↓
OWNER AUTHORIZATION
      ↓
SIX-LAYER VERIFICATION
      ↓
EXECUTION
      ↓
EXECUTION EVIDENCE
الترتيب إلزامي.
لا يجوز:
تنفيذ الفعل قبل التفويض.
تنفيذ الفعل قبل نجاح التحقق.
اعتبار وجود Confirmation وحده كافيًا للتنفيذ.
اعتبار نتيجة Web Research تفويضًا.
تسجيل Success إذا فشل Authorization أو Verification.
تنفيذ side effect جزئي قبل اكتمال Authorization.
## 9. Atomicity وFail-Closed
يجب أن يكون المسار Fail-Closed.
إذا فشلت أي طبقة من:
Classification
Authorization
Verification
Execution Preconditions
فالنتيجة ليست نجاحًا جزئيًا، ولا يجوز إنشاء أثر جانبي خطِر.
الترتيب الذري المطلوب:
Candidate
  → Authorization
  → Verification
  → Execution
  → Evidence
ولا يجوز عكس ترتيب Authorization وExecution.
## 10. Evidence وTrace Safety
يجب أن يوفر التنفيذ دليلًا قابلًا للمراجعة دون كشف الأسرار.
يسمح بتسجيل:
action type.
action fingerprint.
authorization decision.
verification result.
execution result.
correlation/request identifier غير سري.
timestamps اللازمة للتدقيق.
يُمنع تسجيل:
access tokens.
authentication tokens.
credentials.
Owner secrets.
Authorization secrets.
محتوى سري غير مطلوب للتدقيق.
يجب أن يكون Trace قادرًا على إثبات أن الفعل الخطِر مر بالتفويض والتحقق قبل التنفيذ، دون تخزين السر نفسه.
## 11. التوافق مع P6-02
P6-03 يبني فوق آلية P6-02 ولا يعيد تصميمها.
يُمنع بدون قرار مالك مستقل:
حذف AuthorizationService.
حذف DangerousActionPolicy.
تعطيل authorization layer داخل SixLayerVerifier.
جعل install_apk فعلًا آمنًا.
السماح لـ Web Research بمنح Authorization.
تغيير معنى AgentAction جذريًا.
تجاوز SixLayerVerifier.
تغيير عقد Bridge.
تغيير FSM.
إعادة تصميم Web Research.
إضافة dangerous tool جديد.
إنشاء صلاحيات دائمة.
تخزين Owner Confirmation بشكل دائم.
إضافة remote/cloud authorization.
أي تعديل إضافي يجب أن يكون ضروريًا لإغلاق P6-03 ومحدودًا بأقل تغيير ممكن
## 12. الاختبارات المطلوبة
يجب أن يحتوي P6-03 على اختبارات مخصصة تغطي على الأقل:
install_apk مصنف dangerous.
غياب Owner Confirmation يمنع التنفيذ.
Owner denial يمنع التنفيذ.
Confirmation صحيحة ومطابقة تسمح بعبور Authorization.
Confirmation منتهية الصلاحية تُرفض.
request_id مزور يُرفض.
action mismatch يُرفض.
fingerprint mismatch يُرفض.
Confirmation من AuthorizationService مختلف تُرفض.
Web content لا يستطيع إنشاء Authorization صالحة.
Authorization الناجح لا يتجاوز Six-Layer Verification.
فشل Authorization لا ينتج execution success.
Trace لا يحتوي secrets.
المسار Fail-Closed عند أي خطأ Authorization.
Breaker Tests
يجب أن توجد اختبارات كسر مستقلة تحاول:
تمرير confirmed=true من Web Data.
تمرير نص يزعم Owner Approval.
إعادة استخدام Confirmation لفعل مختلف.
تعديل arguments بعد Confirmation.
استخدام Confirmation من service مختلف.
استخدام request ID غير صادر من الخدمة.
استخدام Confirmation منتهية.
تنفيذ dangerous action مباشرة دون verifier.
تسجيل نجاح رغم فشل authorization.
تنفيذ side effect قبل authorization.
تحويل نتيجة Web Research إلى authorization.
تخطي authorization عبر مسار بديل.
## 13. معايير قبول P6-03
يعتبر P6-03 مكتملًا فقط عند تحقق جميع البنود:
P603-01: install_apk dangerous typed action.
P603-02: غياب Owner Confirmation يمنع التنفيذ.
P603-03: رفض المالك يمنع التنفيذ.
P603-04: Confirmation صحيحة ومطابقة تسمح بالعبور.
P603-05: Confirmation منتهية تُرفض.
P603-06: forged request ID يُرفض.
P603-07: action mismatch يُرفض.
P603-08: fingerprint mismatch يُرفض.
P603-09: Confirmation من service مختلف تُرفض.
P603-10: Web Data لا تمنح Authorization.
P603-11: Verification تبقى إلزامية.
P603-12: فشل Authorization لا ينتج execution success.
P603-13: Trace يوفر دليلًا آمنًا.
P603-14: اختبارات P6-03 المخصصة تنجح بالكامل.
P603-15: Breaker tests تنجح بالكامل.
P603-16: Full regression تنجح بالكامل.
P603-17: compileall ينجح.
P603-18: git diff --check ينجح.
P603-19: لا توجد secrets في الملفات أو الاختبارات.
P603-20: لا توجد تغييرات خارج نطاق P6-03.
## 14. General Gates
تظل Gates العامة للمشروع إلزامية:
G1: البناء من الصفر عبر GitHub Actions.
G2: الاختبارات الآلية والأدلة الكاملة.
G3: لا توجد BLOCKER أو MAJOR مفتوحة.
G4: Breaker موثق.
G5: Architect Review موثق.
G6: Owner device test عند الحاجة.
G7: تحديث STATE وtagged commit.
G8: لا توجد secrets.
## 15. Lifecycle
الحالة الحالية عند اعتماد هذه المواصفة:
P6-03 = SPEC
ولا يبدأ BUILD إلا بعد اعتماد المالك للمواصفة.
المسار الإلزامي:
SPEC
→ BUILD
→ BUILT
→ BREAK
→ ARCH_REVIEW
→ FIX
→ REGRESSION
→ OWNER_ACCEPT
→ DONE
لا يجوز إعلان P6-03 DONE قبل Owner Acceptance.
## 16. Minimal Change Principle
يجب إصلاح أو إضافة أقل عدد ممكن من الملفات اللازمة لإغلاق P6-03.
يُمنع إصلاح مشكلة غير مرتبطة إذا كان يمكن أن يغير سلوك P6-01 أو P6-02.
أي تعديل خارج المسار المباشر لـ P6-03 يجب تبريره وتوثيقه.
إذا ظهر تعارض بين:
P6-03.
P6-02.
PROTOCOL.md.
ACCEPTANCE.md.
STATE.md.
فيجب التوقف وعدم التخمين والرجوع إلى قرار المالك.
## 17. Definition of DONE
P6-03 يصبح DONE فقط بعد:
تحقق جميع P603-01 إلى P603-20.
نجاح الاختبارات المخصصة.
نجاح Breaker.
نجاح Full Regression.
نجاح compileall.
نجاح git diff --check.
عدم وجود BLOCKER أو MAJOR.
إكمال Architect Review.
Owner Acceptance صريح.
تحديث STATE.md.
إنشاء commit موثق.
إنشاء tag خاص بإغلاق P6-03.
قبل ذلك تبقى الحالة غير مكتملة.
## 18. القرار المعماري
P6-03 لا يضيف قناة ثقة جديدة.
المصدر الوحيد للتفويض هو Owner Confirmation صادرة عبر AuthorizationService وبمرجعية فعل محدد وبصمة مطابقة.
Web Research يبقى Data فقط.
Verifier يبقى حاجزًا إلزاميًا.
Execution لا يبدأ قبل اكتمال Authorization وVerification.
هذا هو الحد الأمني المعتمد لـ P6-03.
## 19. حالة المواصفة
Phase: 6
Item: P6-03
Status: SPEC
Build: NOT STARTED
Owner Acceptance: PENDING
لا يوجد في هذه المواصفة أي تفويض ضمني ببدء BUILD قبل اعتماد المالك.

## 20. Post-Acceptance Security Correction Record

### 20.1 Finding

After the original P6-03 implementation had passed its acceptance gates, an additional fail-open condition was identified in `agentpro/executor.py`.

When an `AgentAction` explicitly required confirmation through `requires_confirmation=True` but no `confirmation_handler` was supplied, the previous constructor default accepted the action implicitly.

This violated the fail-closed execution principle defined in Sections 6 and 9 of this specification.

### 20.2 Minimal Correction

The default confirmation behavior was changed from implicit approval to explicit denial when no confirmation handler is provided.

Production change:

`confirmation_handler or (lambda _action: False)`

No other production execution logic was changed by this correction.

### 20.3 Breaker Coverage

A dedicated regression test was added:

`test_confirmation_required_action_without_handler_fails_closed`

The test verifies that:

1. an action requiring confirmation is rejected when no handler exists;
2. the execution result is unsuccessful;
3. the underlying action executor receives no execution request.

### 20.4 Verification Evidence

The correction was verified by:

- dedicated breaker test: PASS;
- Phase 6 authorization/verifier/execution-boundary tests: 42/42 PASS;
- full test suite: 221/221 PASS;
- `compileall`: PASS;
- `git diff --check`: PASS;
- placeholder/TODO scan: PASS;
- secret-like scan: PASS;
- production diff limited to the confirmation default;
- test diff limited to the corresponding fail-closed regression test.

### 20.5 Acceptance State

This correction is a post-acceptance security correction to the P6-03 execution boundary.

The correction is **implemented, tested, and architect-reviewed**, but remains:

`OWNER_ACCEPT = PENDING`

No new dangerous tool was introduced, no authorization boundary was bypassed, and no P7 phase transition is authorized by this record alone.
