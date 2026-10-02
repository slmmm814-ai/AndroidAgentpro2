package com.ai.agentpro

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.camera2.CameraAccessException
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import android.util.Size
import androidx.core.content.ContextCompat
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class CameraEngine(
    private val context: Context
) {

    companion object {
        private const val TAG = "AndroidAgentPro.Camera"
        private const val CAPTURE_TIMEOUT_MS = 10_000L

        private const val ERROR_PERMISSION_DENIED = "CAMERA_PERMISSION_DENIED"
        private const val ERROR_NO_CAMERA = "CAMERA_NOT_FOUND"
        private const val ERROR_CAMERA_DISABLED = "CAMERA_DISABLED"
        private const val ERROR_TIMEOUT = "CAMERA_TIMEOUT"
        private const val ERROR_CAPTURE_FAILED = "CAMERA_CAPTURE_FAILED"
        private const val ERROR_ENCODING_FAILED = "CAMERA_ENCODING_FAILED"

        @Volatile
        private var instance: CameraEngine? = null

        fun getInstance(context: Context): CameraEngine {
            return instance ?: synchronized(this) {
                instance ?: CameraEngine(context.applicationContext).also {
                    instance = it
                }
            }
        }
    }

    private val operationCounter = AtomicLong(0L)

    fun capturePhoto(facing: String = "front"): CaptureResult {
        val operationId = operationCounter.incrementAndGet()
        val startedAt = SystemClock.elapsedRealtime()

        val hasPermission = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED

        if (!hasPermission) {
            Log.e(TAG, "Camera permission denied; operation=$operationId")
            return CaptureResult.failure(
                operationId,
                ERROR_PERMISSION_DENIED,
                "Camera permission is not granted to AndroidAgentPro. Please grant CAMERA permission."
            )
        }

        val cameraManager = context.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
        if (cameraManager == null) {
            return CaptureResult.failure(
                operationId,
                ERROR_NO_CAMERA,
                "CameraManager is unavailable on this device"
            )
        }

        val targetFacing = if (facing.equals("back", ignoreCase = true)) {
            CameraCharacteristics.LENS_FACING_BACK
        } else {
            CameraCharacteristics.LENS_FACING_FRONT
        }

        val cameraId = try {
            findCameraId(cameraManager, targetFacing)
        } catch (exception: Exception) {
            Log.e(TAG, "Failed finding camera; operation=$operationId", exception)
            null
        }

        if (cameraId == null) {
            return CaptureResult.failure(
                operationId,
                ERROR_NO_CAMERA,
                "No camera found matching lens facing '$facing'"
            )
        }

        val thread = HandlerThread("CameraCaptureThread-$operationId")
        thread.start()
        val backgroundHandler = Handler(thread.looper)

        val latch = CountDownLatch(1)
        var capturedBytes: ByteArray? = null
        var capturedWidth = 0
        var capturedHeight = 0
        var failureReason: String? = null

        var cameraDevice: CameraDevice? = null
        var imageReader: ImageReader? = null
        var captureSession: CameraCaptureSession? = null

        try {
            val characteristics = cameraManager.getCameraCharacteristics(cameraId)
            val map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val jpegSizes = map?.getOutputSizes(ImageFormat.JPEG)

            val chosenSize = chooseBestSize(jpegSizes)
            capturedWidth = chosenSize.width
            capturedHeight = chosenSize.height

            imageReader = ImageReader.newInstance(
                capturedWidth,
                capturedHeight,
                ImageFormat.JPEG,
                2
            )

            imageReader.setOnImageAvailableListener({ reader ->
                var image: Image? = null
                try {
                    image = reader.acquireNextImage()
                    if (image != null) {
                        val planes = image.planes
                        if (planes.isNotEmpty()) {
                            val buffer = planes[0].buffer
                            val bytes = ByteArray(buffer.remaining())
                            buffer.get(bytes)
                            capturedBytes = bytes
                        }
                    }
                } catch (exception: Exception) {
                    Log.e(TAG, "Error reading image buffer; operation=$operationId", exception)
                    failureReason = exception.message ?: "Failed reading image buffer"
                } finally {
                    image?.close()
                    latch.countDown()
                }
            }, backgroundHandler)

            cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    cameraDevice = camera
                    try {
                        val captureBuilder = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                            addTarget(imageReader.surface)
                            set(
                                CaptureRequest.CONTROL_AF_MODE,
                                CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE
                            )
                            set(
                                CaptureRequest.CONTROL_AE_MODE,
                                CaptureRequest.CONTROL_AE_MODE_ON
                            )
                        }

                        camera.createCaptureSession(
                            listOf(imageReader.surface),
                            object : CameraCaptureSession.StateCallback() {
                                override fun onConfigured(session: CameraCaptureSession) {
                                    captureSession = session
                                    try {
                                        session.capture(
                                            captureBuilder.build(),
                                            null,
                                            backgroundHandler
                                        )
                                    } catch (exception: Exception) {
                                        Log.e(TAG, "Session capture failed; operation=$operationId", exception)
                                        failureReason = "Camera capture failed: ${exception.message}"
                                        latch.countDown()
                                    }
                                }

                                override fun onConfigureFailed(session: CameraCaptureSession) {
                                    Log.e(TAG, "Capture session configuration failed; operation=$operationId")
                                    failureReason = "Camera capture session configuration failed"
                                    latch.countDown()
                                }
                            },
                            backgroundHandler
                        )
                    } catch (exception: Exception) {
                        Log.e(TAG, "Creating capture request failed; operation=$operationId", exception)
                        failureReason = "Creating camera request failed: ${exception.message}"
                        latch.countDown()
                    }
                }

                override fun onDisconnected(camera: CameraDevice) {
                    Log.w(TAG, "Camera disconnected; operation=$operationId")
                    failureReason = "Camera was disconnected"
                    latch.countDown()
                }

                override fun onError(camera: CameraDevice, error: Int) {
                    Log.e(TAG, "Camera device error=$error; operation=$operationId")
                    failureReason = "Camera device error (code $error)"
                    latch.countDown()
                }
            }, backgroundHandler)

        } catch (exception: SecurityException) {
            failureReason = "Camera permission missing or revoked: ${exception.message}"
            latch.countDown()
        } catch (exception: Exception) {
            failureReason = "Camera access exception: ${exception.message}"
            latch.countDown()
        }

        val completed = try {
            latch.await(CAPTURE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (exception: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }

        // Cleanup camera resources
        try {
            captureSession?.close()
        } catch (_: Exception) {}
        try {
            cameraDevice?.close()
        } catch (_: Exception) {}
        try {
            imageReader?.close()
        } catch (_: Exception) {}
        try {
            thread.quitSafely()
        } catch (_: Exception) {}

        val elapsedMs = SystemClock.elapsedRealtime() - startedAt

        if (!completed) {
            Log.e(TAG, "Camera capture timed out; operation=$operationId")
            return CaptureResult.failure(
                operationId,
                ERROR_TIMEOUT,
                "Camera capture timed out after ${CAPTURE_TIMEOUT_MS}ms"
            )
        }

        if (failureReason != null && capturedBytes == null) {
            return CaptureResult.failure(
                operationId,
                ERROR_CAPTURE_FAILED,
                failureReason ?: "Unknown camera failure"
            )
        }

        val bytes = capturedBytes
        if (bytes == null || bytes.isEmpty()) {
            return CaptureResult.failure(
                operationId,
                ERROR_CAPTURE_FAILED,
                "Camera produced empty frame"
            )
        }

        val base64 = try {
            Base64.encodeToString(bytes, Base64.NO_WRAP)
        } catch (exception: Exception) {
            return CaptureResult.failure(
                operationId,
                ERROR_ENCODING_FAILED,
                "Failed to Base64 encode photo: ${exception.message}"
            )
        }

        Log.i(
            TAG,
            "Photo captured successfully; operation=$operationId facing=$facing width=$capturedWidth height=$capturedHeight bytes=${bytes.size} elapsed=${elapsedMs}ms"
        )

        return CaptureResult.success(
            operationId = operationId,
            facing = facing,
            width = capturedWidth,
            height = capturedHeight,
            format = "jpeg",
            byteCount = bytes.size,
            base64 = base64,
            elapsedMs = elapsedMs
        )
    }

    private fun findCameraId(cameraManager: CameraManager, targetFacing: Int): String? {
        val cameraIds = cameraManager.cameraIdList
        for (id in cameraIds) {
            val characteristics = cameraManager.getCameraCharacteristics(id)
            val lensFacing = characteristics.get(CameraCharacteristics.LENS_FACING)
            if (lensFacing == targetFacing) {
                return id
            }
        }
        return cameraIds.firstOrNull()
    }

    private fun chooseBestSize(sizes: Array<Size>?): Size {
        if (sizes.isNullOrEmpty()) {
            return Size(1280, 720)
        }

        // Prefer sizes around 1280x720 or 1920x1080 for quick transmission & quality
        val preferred = sizes.filter { it.width in 640..1920 && it.height in 480..1080 }
        if (preferred.isNotEmpty()) {
            return preferred.maxByOrNull { it.width * it.height } ?: sizes[0]
        }

        return sizes.first()
    }

    data class CaptureResult(
        val operationId: Long,
        val success: Boolean,
        val code: String?,
        val message: String?,
        val facing: String?,
        val width: Int,
        val height: Int,
        val format: String?,
        val byteCount: Int,
        val base64: String?,
        val elapsedMs: Long
    ) {
        companion object {
            fun success(
                operationId: Long,
                facing: String,
                width: Int,
                height: Int,
                format: String,
                byteCount: Int,
                base64: String,
                elapsedMs: Long
            ): CaptureResult {
                return CaptureResult(
                    operationId = operationId,
                    success = true,
                    code = null,
                    message = null,
                    facing = facing,
                    width = width,
                    height = height,
                    format = format,
                    byteCount = byteCount,
                    base64 = base64,
                    elapsedMs = elapsedMs
                )
            }

            fun failure(
                operationId: Long,
                code: String,
                message: String
            ): CaptureResult {
                return CaptureResult(
                    operationId = operationId,
                    success = false,
                    code = code,
                    message = message,
                    facing = null,
                    width = 0,
                    height = 0,
                    format = null,
                    byteCount = 0,
                    base64 = null,
                    elapsedMs = 0L
                )
            }
        }
    }
}
