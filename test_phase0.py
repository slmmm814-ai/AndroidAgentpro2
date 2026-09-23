from agentpro.bridge import click, status

print("⏳ جاري فحص الاتصال بالتطبيق...")
result = status()
print("📡 نتيجة الفحص:", result)

if result == "ready":
    print("\n🎉🎉 نجح الاتصال! التطبيق يستجيب.")
    print("👉 سأقوم بالنقر على منتصف الشاشة (X=540, Y=1000) كتجربة...")
    print("🔍 نتيجة النقر:", click(540, 1000))
else:
    print("\n❌ التطبيق لا يستجيب. تأكد من:")
    print("1. تفعيل صلاحية إمكانية الوصول لـ AgentBridge")
    print("2. أن التطبيق يعمل في الخلفية")
