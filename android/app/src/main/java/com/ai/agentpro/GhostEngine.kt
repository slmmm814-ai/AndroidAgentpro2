package com.ai.agentpro

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.os.ResultReceiver
import android.util.Log
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
    private var mediaProjection: MediaProjection? = null
    private val displayManager = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
    private val activeDisplayId = AtomicInteger(-1)

    /**
     * Create the ghost display using a MediaProjection token so that external
     * apps are allowed to render onto the virtual display.
     *
     * [resultCode]/[data] come from the MediaProjection consent dialog. If the
     * projection token is missing the display cannot host other apps, so this
     * returns -1.
     */
    fun createGhostDisplay(
        width: Int = DEFAULT_WIDTH,
        height: Int = DEFAULT_HEIGHT,
        dpi: Int = DEFAULT_DPI,
        resultCode: Int = Activity.RESULT_CANCELED,
        data: Intent? = null
    ): Int {
        if (virtualDisplay != null) {
            return activeDisplayId.get()
        }

        // Reuse the existing virtual display if we already have one.
        // Android forbids creating multiple displays on the same MediaProjection.
        if (virtualDisplay != null) {
            val existingId = activeDisplayId.get()
            if (existingId != -1) {
                Log.i(TAG, "Reusing existing ghost display: ID=$existingId")
                return existingId
            }
        }

        try {
            if (mediaProjection == null) {
                if (data == null || resultCode != Activity.RESULT_OK) {
                    Log.e(TAG, "MediaProjection consent missing; cannot host other apps")
                    return -1
                }

                readerThread = HandlerThread("GhostImageReader").also { it.start() }
                readerHandler = Handler(readerThread!!.looper)

                val projectionManager =
                    context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                mediaProjection = projectionManager.getMediaProjection(resultCode, data)

                // Android 14+ requires a callback to be registered before capture starts.
                mediaProjection?.registerCallback(object : MediaProjection.Callback() {
                    override fun onStop() {
                        Log.i(TAG, "MediaProjection stopped")
                        mediaProjection = null
                    }
                }, readerHandler)
            } else {
                Log.i(TAG, "Reusing existing MediaProjection")
                if (readerHandler == null) {
                    readerThread = HandlerThread("GhostImageReader").also { it.start() }
                    readerHandler = Handler(readerThread!!.looper)
                }
            }

            imageReader = ImageReader.newInstance(
                width,
                height,
                PixelFormat.RGBA_8888,
                3
            )

            // NOTE: no OnImageAvailableListener here - we must NOT drain frames,
            // otherwise captureGhostDisplay() finds nothing. The reader buffers up
            // to `maxImages` frames and acquireLatestImage() returns the newest one.

            val surface = imageReader!!.surface

            // MediaProjection token allows a PUBLIC display with other apps' content.
            virtualDisplay = mediaProjection!!.createVirtualDisplay(
                "GhostDisplay",
                width,
                height,
                dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC or
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                surface,
                null,
                null
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
        // Keep the virtual display and projection alive; just mark inactive.
        // Android forbids re-creating displays on the same MediaProjection instance,
        // so the display is created once and reused for every ghost session.
        activeDisplayId.set(virtualDisplay?.display?.displayId ?: -1)
        Log.i(TAG, "Ghost display hidden (kept alive)")
    }

    fun resumeGhostDisplay(): Int {
        return activeDisplayId.get()
    }

    fun stopProjection() {
        virtualDisplay?.release()
        virtualDisplay = null
        activeDisplayId.set(-1)
        cleanupReader()
        try {
            mediaProjection?.stop()
        } catch (_: Exception) {
        }
        mediaProjection = null
        Log.i(TAG, "MediaProjection stopped")
    }

    fun isGhostModeActive(): Boolean = virtualDisplay != null
}
