package com.ai.agentpro

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Display
import java.io.ByteArrayOutputStream
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
    private var imageReader: ImageReader? = null
    private var readerThread: HandlerThread? = null
    private var readerHandler: Handler? = null
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
    private val activeDisplayId = AtomicInteger(-1)

    fun createGhostDisplay(width: Int = DEFAULT_WIDTH, height: Int = DEFAULT_HEIGHT, dpi: Int = DEFAULT_DPI): Int {
        if (virtualDisplay != null) {
            return activeDisplayId.get()
        }

        try {
            // Background thread for ImageReader callbacks
            readerThread = HandlerThread("GhostImageReader").also { it.start() }
            readerHandler = Handler(readerThread!!.looper)

            // ImageReader provides the surface so the virtual display actually renders,
            // which lets us capture screenshots and keeps windows alive.
            imageReader = ImageReader.newInstance(
                width,
                height,
                PixelFormat.RGBA_8888,
                2
            )

            imageReader?.setOnImageAvailableListener({ reader ->
                // Acquire and discard to keep the buffer flowing
                try {
                    val img = reader.acquireLatestImage()
                    img?.close()
                } catch (_: Exception) {
                }
            }, readerHandler)

            val surface = imageReader!!.surface

            // Android requires OWN_CONTENT_ONLY for non-screen-sharing virtual displays
            // created without MediaProjection. Combined with PUBLIC this still lets the
            // display host other apps' windows once they are launched onto it.
            virtualDisplay = displayManager.createVirtualDisplay(
                "GhostDisplay",
                width,
                height,
                dpi,
                surface,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC or
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY
            )

            val displayId = virtualDisplay?.display?.displayId ?: -1
            activeDisplayId.set(displayId)

            Log.i(TAG, "Ghost Display created: ID=$displayId")
            return displayId
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create Ghost Display", e)
            cleanupReader()
            return -1
        }
    }

    /**
     * Capture the latest frame from the ghost display as JPEG bytes, or null on failure.
     */
    fun captureGhostDisplay(quality: Int = 80): ByteArray? {
        val reader = imageReader ?: return null
        return try {
            val image: Image = reader.acquireLatestImage() ?: return null
            image.use {
                val planes = it.planes
                val buffer = planes[0].buffer
                val pixelStride = planes[0].pixelStride
                val rowStride = planes[0].rowStride
                val rowPadding = rowStride - pixelStride * it.width

                val bitmap = Bitmap.createBitmap(
                    it.width + rowPadding / pixelStride,
                    it.height,
                    Bitmap.Config.ARGB_8888
                )
                bitmap.copyPixelsFromBuffer(buffer)

                // Crop padding if present
                val cropped = if (rowPadding > 0) {
                    Bitmap.createBitmap(bitmap, 0, 0, it.width, it.height)
                } else {
                    bitmap
                }

                val out = ByteArrayOutputStream()
                cropped.compress(Bitmap.CompressFormat.JPEG, quality, out)
                out.toByteArray()
            }
        } catch (e: Exception) {
            Log.e(TAG, "captureGhostDisplay failed", e)
            null
        }
    }

    fun getGhostDisplayId(): Int = activeDisplayId.get()

    fun isGhostDisplay(displayId: Int): Boolean = displayId == activeDisplayId.get() && activeDisplayId.get() != -1

    private fun cleanupReader() {
        try {
            imageReader?.close()
        } catch (_: Exception) {
        }
        imageReader = null
        try {
            readerThread?.quitSafely()
        } catch (_: Exception) {
        }
        readerThread = null
        readerHandler = null
    }

    fun releaseGhostDisplay() {
        virtualDisplay?.release()
        virtualDisplay = null
        activeDisplayId.set(-1)
        cleanupReader()
        Log.i(TAG, "Ghost Display released")
    }

    fun isGhostModeActive(): Boolean = virtualDisplay != null
}
