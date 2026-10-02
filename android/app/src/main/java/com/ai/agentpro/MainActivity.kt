package com.ai.agentpro

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

class MainActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        ForegroundWatchdogService.start(applicationContext)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 64, 48, 48)
        }

        val title = TextView(this).apply {
            text = "AndroidAgentPro"
            textSize = 24f
        }

        val status = TextView(this).apply {
            text = "Phase 0 — Bridge foundation"
            textSize = 16f
        }

        val accessibilityButton = Button(this).apply {
            text = "فتح إعدادات إمكانية الوصول"
            setOnClickListener {
                startActivity(
                    Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                )
            }
        }

        val copyTokenButton = Button(this).apply {
            text = "نسخ رمز Bridge"
            setOnClickListener {
                try {
                    val token = BridgeProtocol.getOrCreateToken(applicationContext)

                    val clipboard =
                        getSystemService(Context.CLIPBOARD_SERVICE)
                            as? ClipboardManager
                            ?: throw IllegalStateException(
                                "Clipboard service is unavailable"
                            )

                    clipboard.setPrimaryClip(
                        ClipData.newPlainText(
                            "AndroidAgentPro Bridge Token",
                            token
                        )
                    )

                    status.text = "تم نسخ رمز Bridge إلى الحافظة"
                } catch (exception: Exception) {
                    status.text = "فشل نسخ رمز Bridge"
                    android.util.Log.e(
                        "AndroidAgentPro.Main",
                        "Unable to copy bridge token",
                        exception
                    )
                }
            }
        }

        val shizukuButton = Button(this).apply {
            text = "تفعيل صلاحيات النظام (Shizuku)"
            setOnClickListener {
                val engine = ShizukuEngine.getInstance(applicationContext)
                if (!engine.isServiceAvailable()) {
                    status.text = "خدمة Shizuku غير مشغلة! يرجى تشغيل تطبيق Shizuku أولاً."
                } else if (engine.hasPermission()) {
                    status.text = "صلاحيات Shizuku مفعلة بالفعل ✅"
                } else {
                    // Shizuku logic is async, we just trigger the request
                    try {
                        rikka.shizuku.Shizuku.requestPermission(1001)
                        status.text = "تم طلب صلاحية Shizuku..."
                    } catch (e: Exception) {
                        status.text = "فشل طلب الصلاحية: ${e.message}"
                    }
                }
            }
        }

        val ghostButton = Button(this).apply {
            text = "تفعيل وضع الشبح (Ghost Mode)"
            setOnClickListener {
                val projectionManager =
                    getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                        as? android.media.projection.MediaProjectionManager
                if (projectionManager == null) {
                    status.text = "MediaProjection غير مدعوم"
                    return@setOnClickListener
                }
                @Suppress("DEPRECATION")
                startActivityForResult(
                    projectionManager.createScreenCaptureIntent(),
                    GHOST_PROJECTION_REQUEST
                )
            }
        }

        root.addView(title)
        root.addView(status)
        root.addView(accessibilityButton)
        root.addView(copyTokenButton)
        root.addView(shizukuButton)
        root.addView(ghostButton)

        setContentView(root)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode == GHOST_PROJECTION_REQUEST) {
            if (resultCode == RESULT_OK && data != null) {
                // Hand the projection token to the ghost engine
                GhostProjectionHolder.resultCode = resultCode
                GhostProjectionHolder.data = data
                status.text = "تم منح إذن الشبح ✅ اضغط مرة أخرى لتفعيله"
            } else {
                status.text = "تم رفض إذن الشبح"
            }
        }
    }

    companion object {
        private const val GHOST_PROJECTION_REQUEST = 2001
    }
}

/**
 * Holds the MediaProjection consent result until the bridge asks for it.
 */
object GhostProjectionHolder {
    @Volatile var resultCode: Int = android.app.Activity.RESULT_CANCELED
    @Volatile var data: Intent? = null
}