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

        root.addView(title)
        root.addView(status)
        root.addView(accessibilityButton)
        root.addView(copyTokenButton)

        setContentView(root)
    }
}
