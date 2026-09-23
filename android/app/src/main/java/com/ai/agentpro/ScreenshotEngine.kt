package com.ai.agentpro

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.os.Build
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class ScreenshotEngine(
    private val service: AccessibilityService
) {

    companion object {
        private const val TAG = "AndroidAgentPro.Screenshot"
        private const val CAPTURE_TIMEOUT_MS = 8_000L
        private const val JPEG_QUALITY = 85
        private const val MIN_CAPTURE_INTERVAL_MS = 250L

        private const val ERROR_API_UNSUPPORTED = "SCREENSHOT_API_UNSUPPORTED"
        private const val ERROR_TIMEOUT = "SCREENSHOT_TIMEOUT"
        private const val ERROR_CAPTURE_FAILED = "SCREENSHOT_CAPTURE_FAILED"
        private const val ERROR_EMPTY_RESULT = "SCREENSHOT_EMPTY_RESULT"
        private const val ERROR_ENCODING_FAILED = "SCREENSHOT_ENCODING_FAILED"

        @Volatile
        private var instance: ScreenshotEngine? = null

        fun install(service: AccessibilityService): ScreenshotEngine {
            val engine = ScreenshotEngine(service)
            instance = engine
            return engine
        }

        fun getInstance(): ScreenshotEngine? {
            return instance
        }

        fun clear(service: AccessibilityService) {
            if (instance?.service === service) {
                instance = null
            }
        }
    }

    private val operationCounter = AtomicLong(0L)

    @Volatile
    private var lastCaptureStartedAt = 0L

    fun captureDefaultDisplay(): CaptureResult {
        val operationId = operationCounter.incrementAndGet()
        val startedAt = SystemClock.elapsedRealtime()

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return CaptureResult.failure(
                operationId,
                ERROR_API_UNSUPPORTED,
                "Accessibility screenshot requires Android 11/API 30 or newer"
            )
        }

        val now = SystemClock.elapsedRealtime()
        val previous = lastCaptureStartedAt

        if (previous != 0L) {
            val elapsed = now - previous

            if (elapsed < MIN_CAPTURE_INTERVAL_MS) {
                Log.w(
                    TAG,
                    "Screenshot rate limited; operation=$operationId"
                )

                return CaptureResult.failure(
                    operationId,
                    ERROR_CAPTURE_FAILED,
                    "Screenshot requested too frequently"
                )
            }
        }

        lastCaptureStartedAt = now

        val latch = CountDownLatch(1)

        var bitmap: Bitmap? = null
        var failureCode: Int? = null

        try {
            service.takeScreenshot(
                android.view.Display.DEFAULT_DISPLAY,
                service.mainExecutor,
                object : AccessibilityService.TakeScreenshotCallback {

                    override fun onSuccess(
                        screenshot: AccessibilityService.ScreenshotResult
                    ) {
                        try {
                            val hardwareBuffer = screenshot.hardwareBuffer

                            if (hardwareBuffer == null) {
                                Log.e(
                                    TAG,
                                    "Screenshot returned null HardwareBuffer; operation=$operationId"
                                )
                                return
                            }

                            try {
                                val hardwareBitmap = Bitmap.wrapHardwareBuffer(
                                    hardwareBuffer,
                                    screenshot.colorSpace
                                )

                                if (hardwareBitmap == null) {
                                    Log.e(
                                        TAG,
                                        "Bitmap conversion returned null; operation=$operationId"
                                    )
                                    return
                                }

                                bitmap = hardwareBitmap.copy(
                                    Bitmap.Config.ARGB_8888,
                                    false
                                )

                                hardwareBitmap.recycle()
                            } finally {
                                hardwareBuffer.close()
                            }
                        } catch (exception: Exception) {
                            Log.e(
                                TAG,
                                "Unable to convert screenshot buffer; operation=$operationId",
                                exception
                            )
                        } finally {
                            latch.countDown()
                        }
                    }

                    override fun onFailure(errorCode: Int) {
                        failureCode = errorCode

                        Log.e(
                            TAG,
                            "Screenshot failed; operation=$operationId errorCode=$errorCode"
                        )

                        latch.countDown()
                    }
                }
            )
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "Screenshot request failed; operation=$operationId",
                exception
            )

            return CaptureResult.failure(
                operationId,
                ERROR_CAPTURE_FAILED,
                exception.message ?: exception.javaClass.simpleName
            )
        }

        val completed = try {
            latch.await(
                CAPTURE_TIMEOUT_MS,
                TimeUnit.MILLISECONDS
            )
        } catch (exception: InterruptedException) {
            Thread.currentThread().interrupt()

            return CaptureResult.failure(
                operationId,
                ERROR_TIMEOUT,
                "Screenshot wait was interrupted"
            )
        }

        if (!completed) {
            Log.e(
                TAG,
                "Screenshot timed out; operation=$operationId"
            )

            return CaptureResult.failure(
                operationId,
                ERROR_TIMEOUT,
                "Screenshot callback timed out after ${CAPTURE_TIMEOUT_MS}ms"
            )
        }

        val capturedBitmap = bitmap

        if (capturedBitmap == null) {
            return CaptureResult.failure(
                operationId,
                ERROR_EMPTY_RESULT,
                "Screenshot callback completed without a bitmap; systemError=$failureCode"
            )
        }

        return try {
            encodeBitmap(
                operationId,
                capturedBitmap,
                startedAt
            )
        } finally {
            capturedBitmap.recycle()
        }
    }

    private fun encodeBitmap(
        operationId: Long,
        bitmap: Bitmap,
        startedAt: Long
    ): CaptureResult {

        if (bitmap.width <= 0 || bitmap.height <= 0) {
            return CaptureResult.failure(
                operationId,
                ERROR_EMPTY_RESULT,
                "Screenshot bitmap has invalid dimensions"
            )
        }

        val output = ByteArrayOutputStream()

        val compressed = try {
            bitmap.compress(
                Bitmap.CompressFormat.JPEG,
                JPEG_QUALITY,
                output
            )
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "JPEG encoding failed; operation=$operationId",
                exception
            )
            false
        }

        if (!compressed) {
            return CaptureResult.failure(
                operationId,
                ERROR_ENCODING_FAILED,
                "Bitmap JPEG compression failed"
            )
        }

        val bytes = try {
            output.toByteArray()
        } finally {
            try {
                output.close()
            } catch (exception: Exception) {
                Log.w(
                    TAG,
                    "Unable to close screenshot output stream",
                    exception
                )
            }
        }

        if (bytes.isEmpty()) {
            return CaptureResult.failure(
                operationId,
                ERROR_ENCODING_FAILED,
                "Encoded screenshot is empty"
            )
        }

        val base64 = try {
            Base64.encodeToString(
                bytes,
                Base64.NO_WRAP
            )
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "Base64 encoding failed; operation=$operationId",
                exception
            )

            return CaptureResult.failure(
                operationId,
                ERROR_ENCODING_FAILED,
                "Unable to encode screenshot as Base64"
            )
        }

        val elapsedMs =
            SystemClock.elapsedRealtime() - startedAt

        Log.i(
            TAG,
            "Screenshot captured successfully; " +
                "operation=$operationId " +
                "width=${bitmap.width} " +
                "height=${bitmap.height} " +
                "bytes=${bytes.size} " +
                "elapsed=${elapsedMs}ms"
        )

        return CaptureResult.success(
            operationId,
            bitmap.width,
            bitmap.height,
            "jpeg",
            JPEG_QUALITY,
            bytes.size,
            base64,
            elapsedMs
        )
    }

    data class CaptureResult(
        val operationId: Long,
        val success: Boolean,
        val code: String?,
        val message: String?,
        val width: Int,
        val height: Int,
        val format: String?,
        val quality: Int?,
        val byteCount: Int,
        val base64: String?,
        val elapsedMs: Long
    ) {
        companion object {

            fun success(
                operationId: Long,
                width: Int,
                height: Int,
                format: String,
                quality: Int,
                byteCount: Int,
                base64: String,
                elapsedMs: Long
            ): CaptureResult {
                return CaptureResult(
                    operationId,
                    true,
                    null,
                    null,
                    width,
                    height,
                    format,
                    quality,
                    byteCount,
                    base64,
                    elapsedMs
                )
            }

            fun failure(
                operationId: Long,
                code: String,
                message: String
            ): CaptureResult {
                return CaptureResult(
                    operationId,
                    false,
                    code,
                    message,
                    0,
                    0,
                    null,
                    null,
                    0,
                    null,
                    0L
                )
            }
        }
    }
}
