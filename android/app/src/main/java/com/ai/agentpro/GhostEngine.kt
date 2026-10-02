package com.ai.agentpro

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Display
import java.util.concurrent.atomic.AtomicInteger

class GhostEngine(private val context: Context) {

    companion object {
        private const val TAG = "AndroidAgentPro.Ghost"
        private const val DEFAULT_WIDTH = 1080
        private const val DEFAULT_HEIGHT = 2340
        private const val DEFAULT_DPI = 440

        @Volatile
        private var instance: GhostEngine? = null

        fun getInstance(context: Context): GhostEngine {
            return instance ?: synchronized(this) {
                instance ?: GhostEngine(context.applicationContext).also { instance = it }
            }
        }
    }

    private var virtualDisplay: VirtualDisplay? = null
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
    private val activeDisplayId = AtomicInteger(-1)

    fun createGhostDisplay(width: Int = DEFAULT_WIDTH, height: Int = DEFAULT_HEIGHT, dpi: Int = DEFAULT_DPI): Int {
        if (virtualDisplay != null) {
            return activeDisplayId.get()
        }

        try {
            // We use Shizuku/ADB power to create an overlay display via settings if needed,
            // or use the standard DisplayManager if permissions allow.
            // For a true Ghost Mode, we create a private virtual display.
            virtualDisplay = displayManager.createVirtualDisplay(
                "GhostDisplay",
                width,
                height,
                dpi,
                null, // No surface needed for logic-only processing, or we can attach an ImageReader
                DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY or DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
            )

            val displayId = virtualDisplay?.display?.displayId ?: -1
            activeDisplayId.set(displayId)
            
            Log.i(TAG, "Ghost Display created: ID=$displayId")
            return displayId
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create Ghost Display", e)
            return -1
        }
    }

    fun getGhostDisplayId(): Int = activeDisplayId.get()

    fun releaseGhostDisplay() {
        virtualDisplay?.release()
        virtualDisplay = null
        activeDisplayId.set(-1)
        Log.i(TAG, "Ghost Display released")
    }

    fun isGhostModeActive(): Boolean = virtualDisplay != null
}
