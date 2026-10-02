package com.ai.agentpro

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Base64
import android.util.Log
import androidx.core.content.FileProvider
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.File
import java.io.IOException
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class BridgeServer(
    private val context: Context
) {

    companion object {
        private const val TAG = "AndroidAgentPro.Bridge"
        private const val MAX_HEADER_BYTES = 16 * 1024
        private const val MAX_BODY_BYTES = 2 * 1024 * 1024
        private const val SOCKET_TIMEOUT_MS = 10_000
        private const val GESTURE_TIMEOUT_MS = 5_000L
        private const val SERVER_BACKLOG = 16
        private const val WORKER_COUNT = 4

        @Volatile
        private var instance: BridgeServer? = null

        fun getInstance(context: Context): BridgeServer {
            return instance ?: synchronized(this) {
                instance ?: BridgeServer(
                    context.applicationContext
                ).also {
                    instance = it
                }
            }
        }
    }

    private val running = AtomicBoolean(false)

    @Volatile
    private var workers: java.util.concurrent.ExecutorService? = null

    @Volatile
    private var commandExecutor: java.util.concurrent.ExecutorService? = null

    @Volatile
    private var serverSocket: ServerSocket? = null

    @Volatile
    private var acceptThread: Thread? = null

    private val authenticationToken: String by lazy {
        BridgeProtocol.getOrCreateToken(context)
    }

    fun start() {
        if (running.get()) {
            Log.i(TAG, "Bridge server already running")
            return
        }

        synchronized(this) {
            if (running.get()) {
                return
            }

            try {
                val socket = ServerSocket(
                    BridgeProtocol.SERVER_PORT,
                    SERVER_BACKLOG,
                    InetAddress.getByName(
                        BridgeProtocol.SERVER_HOST
                    )
                )

                socket.soTimeout = 1_000

                workers = Executors.newFixedThreadPool(
                    WORKER_COUNT
                )

                commandExecutor = Executors.newCachedThreadPool()

                serverSocket = socket
                running.set(true)

                val thread = Thread(
                    {
                        acceptLoop()
                    },
                    "AndroidAgentPro-BridgeAccept"
                )

                thread.isDaemon = true
                acceptThread = thread
                thread.start()

                Log.i(
                    TAG,
                    "Bridge server started on " +
                        "${BridgeProtocol.SERVER_HOST}:${BridgeProtocol.SERVER_PORT}"
                )
            } catch (exception: Exception) {
                running.set(false)

                try {
                    serverSocket?.close()
                } catch (_: Exception) {
                }

                serverSocket = null

                Log.e(
                    TAG,
                    "Unable to start bridge server",
                    exception
                )

                throw exception
            }
        }
    }

    fun stop() {
        synchronized(this) {
            if (!running.getAndSet(false)) {
                return
            }

            Log.i(TAG, "Stopping bridge server")

            try {
                serverSocket?.close()
            } catch (exception: IOException) {
                Log.w(
                    TAG,
                    "Error while closing bridge socket",
                    exception
                )
            }

            serverSocket = null

            acceptThread?.interrupt()
            acceptThread = null

            workers?.shutdownNow()
            workers = null

            commandExecutor?.shutdownNow()
            commandExecutor = null

            Log.i(TAG, "Bridge server stopped")
        }
    }

    fun isRunning(): Boolean {
        return running.get() &&
            serverSocket?.isClosed == false
    }


    private fun acceptLoop() {
        while (running.get()) {
            try {
                val socket = serverSocket?.accept()
                    ?: break

                socket.soTimeout = SOCKET_TIMEOUT_MS

                val executor = workers

                if (executor == null || executor.isShutdown) {
                    Log.w(
                        TAG,
                        "Rejecting client because worker executor is unavailable"
                    )
                    socket.close()
                    continue
                }

                executor.execute {
                    handleClient(socket)
                }
            } catch (_: java.net.SocketTimeoutException) {
                continue
            } catch (exception: SocketException) {
                if (running.get()) {
                    Log.e(
                        TAG,
                        "Bridge accept loop socket failure",
                        exception
                    )
                }
            } catch (exception: Exception) {
                if (running.get()) {
                    Log.e(
                        TAG,
                        "Bridge accept loop failure",
                        exception
                    )
                }
            }
        }

        Log.i(TAG, "Bridge accept loop stopped")
    }

    private fun handleClient(socket: Socket) {
        socket.use {
            try {
                val request = readHttpRequest(socket)

                if (request.method != "POST") {
                    writeHttpResponse(
                        socket,
                        405,
                        BridgeProtocol.error(
                            request.requestId,
                            "METHOD_NOT_ALLOWED",
                            "Only POST is supported"
                        )
                    )
                    return
                }

                if (request.path != "/v1/command") {
                    writeHttpResponse(
                        socket,
                        404,
                        BridgeProtocol.error(
                            request.requestId,
                            "NOT_FOUND",
                            "Endpoint not found"
                        )
                    )
                    return
                }

                if (!secureEquals(
                        authenticationToken,
                        request.authorizationToken
                    )
                ) {
                    Log.w(TAG, "Unauthorized bridge request")

                    writeHttpResponse(
                        socket,
                        401,
                        BridgeProtocol.error(
                            request.requestId,
                            "UNAUTHORIZED",
                            "Authentication failed"
                        )
                    )
                    return
                }

                val bridgeRequest = try {
                    BridgeProtocol.parseRequest(
                        request.body
                    )
                } catch (exception: BridgeProtocol.BridgeProtocolException) {
                    writeHttpResponse(
                        socket,
                        400,
                        BridgeProtocol.error(
                            request.requestId.ifBlank {
                                BridgeProtocol.newRequestId()
                            },
                            exception.code,
                            exception.message
                        )
                    )
                    return
                }

                val response = executeCommand(
                    bridgeRequest
                )

                writeHttpResponse(
                    socket,
                    if (response.ok) 200 else response.httpCode,
                    response.json
                )
            } catch (exception: Exception) {
                Log.e(
                    TAG,
                    "Client handling failed",
                    exception
                )

                try {
                    writeHttpResponse(
                        socket,
                        500,
                        BridgeProtocol.error(
                            BridgeProtocol.newRequestId(),
                            "INTERNAL_ERROR",
                            "Internal bridge error"
                        )
                    )
                } catch (_: Exception) {
                }
            }
        }
    }

    private fun executeCommand(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        return try {
            when (request.command) {
                "health" -> {
                    val accessibility =
                        AgentAccessibilityService.getInstance(
                        )

                    CommandResponse.success(
                        BridgeProtocol.success(
                            request.requestId,
                            JSONObject()
                                .put(
                                    "server_running",
                                    isRunning()
                                )
                                .put(
                                    "accessibility_connected",
                                    accessibility?.isConnected()
                                        == true
                                )
                                .put(
                                    "version_name",
                                    BuildConfig.VERSION_NAME
                                )
                                .put(
                                    "version_code",
                                    BuildConfig.VERSION_CODE
                                )
                                .put(
                                    "build_tag",
                                    BuildConfig.BUILD_TAG
                                )
                        )
                    )
                }

                "ui_dump" -> {
                    val accessibility =
                        AgentAccessibilityService.getInstance()

                    if (accessibility == null ||
                        !accessibility.isConnected()
                    ) {
                        return CommandResponse.failure(
                            503,
                            BridgeProtocol.error(
                                request.requestId,
                                "ACCESSIBILITY_NOT_CONNECTED",
                                "Accessibility service is not connected"
                            )
                        )
                    }

                    val packageName =
                        request.args.optString("package", "").trim()

                    val result = if (packageName.isEmpty()) {
                        accessibility.dumpUi()
                    } else {
                        accessibility.dumpUiForPackage(packageName)
                    }

                    if (!result.success || result.root == null) {
                        CommandResponse.failure(
                            503,
                            BridgeProtocol.error(
                                request.requestId,
                                result.errorCode
                                    ?: "UI_DUMP_FAILED",
                                result.errorMessage
                                    ?: "UI dump failed"
                            )
                        )
                    } else {
                        CommandResponse.success(
                            BridgeProtocol.success(
                                request.requestId,
                                JSONObject()
                                    .put(
                                        "root",
                                        BridgeProtocol.uiNodeToJson(
                                            result.root
                                        )
                                    )
                                    .put(
                                        "truncated",
                                        result.truncated
                                    )
                            )
                        )
                    }
                }

                "list_windows" -> executeListWindows(request)

                "node_action" -> executeNodeAction(request)

                "screenshot" -> executeScreenshot(request)

                "camera_capture",                 "take_photo" -> executeCameraCapture(request)

                "shizuku_status" -> executeShizukuStatus(request)
                "shizuku_shell" -> executeShizukuShell(request)

                "ghost_start" -> executeGhostStart(request)
                "ghost_stop" -> executeGhostStop(request)
                "ghost_screenshot" -> executeGhostScreenshot(request)

                "visual_hash" -> executeVisualHash(request)

                "tap" -> executeTap(request)

                "back" -> executeBack(request)

                "input_text" -> executeInputText(request)

                "swipe" -> executeSwipe(request)

                "long_press" -> executeLongPress(request)

                "clear_text" -> executeClearText(request)

                "erase_text" -> executeEraseText(request)

                "get_window" -> executeWindowInfo(request)

                "key_event" -> executeKeyEvent(request)

                "open_url" -> executeOpenUrl(request)

                "launch_app" -> executeLaunchApp(request)

                else -> {
                    CommandResponse.failure(
                        400,
                        BridgeProtocol.error(
                            request.requestId,
                            "UNKNOWN_COMMAND",
                            "Unsupported command: ${request.command}"
                        )
                    )
                }
            }
        } catch (exception: Exception) {
            Log.e(
                TAG,
                "Command execution failed: ${request.command}",
                exception
            )

            CommandResponse.failure(
                500,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTION_FAILED",
                    exception.message
                        ?: "Command execution failed"
                )
            )
        }
    }

    /**
     * Returns a compact perceptual hash of the current screen.
     *
     * Answers "did anything move" with a 64-character string instead of a
     * 300-400 KB JPEG, so the caller can measure screen change on every step
     * without JPEG artefacts or screenshot rate limiting getting in the way.
     */
    private fun executeVisualHash(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val engine = ScreenshotEngine.getInstance()

        if (engine == null) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "SCREENSHOT_ENGINE_NOT_READY",
                    "Screenshot engine is not initialized"
                )
            )
        }

        val result = engine.captureVisualHash()

        if (!result.success || result.hash == null) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.code ?: "VISUAL_HASH_FAILED",
                    result.message ?: "Visual hash capture failed"
                )
            )
        }

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put(
                        "operation_id",
                        result.operationId
                    )
                    .put(
                        "hash",
                        result.hash
                    )
            )
        )
    }

    private fun executeScreenshot(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val engine = ScreenshotEngine.getInstance()

        if (engine == null) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "SCREENSHOT_ENGINE_NOT_READY",
                    "Screenshot engine is not initialized"
                )
            )
        }

        val result = engine.captureDefaultDisplay()

        if (!result.success || result.base64 == null) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.code ?: "SCREENSHOT_FAILED",
                    result.message ?: "Screenshot capture failed"
                )
            )
        }

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put(
                        "operation_id",
                        result.operationId
                    )
                    .put(
                        "width",
                        result.width
                    )
                    .put(
                        "height",
                        result.height
                    )
                    .put(
                        "format",
                        result.format ?: "jpeg"
                    )
                    .put(
                        "quality",
                        result.quality ?: 85
                    )
                    .put(
                        "byte_count",
                        result.byteCount
                    )
                    .put(
                        "base64",
                        result.base64
                    )
                    .put(
                        "elapsed_ms",
                        result.elapsedMs
                    )
            )
        )
    }

    private fun executeCameraCapture(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val facing = request.args.optString("facing", "front")
        val engine = CameraEngine.getInstance(context)

        val result = engine.capturePhoto(facing = facing)

        if (!result.success || result.base64 == null) {
            val status = if (result.code == "CAMERA_PERMISSION_DENIED") 403 else 503
            return CommandResponse.failure(
                status,
                BridgeProtocol.error(
                    request.requestId,
                    result.code ?: "CAMERA_CAPTURE_FAILED",
                    result.message ?: "Camera photo capture failed"
                )
            )
        }

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put("operation_id", result.operationId)
                    .put("facing", result.facing ?: facing)
                    .put("width", result.width)
                    .put("height", result.height)
                    .put("format", result.format ?: "jpeg")
                    .put("byte_count", result.byteCount)
                    .put("base64", result.base64)
                    .put("elapsed_ms", result.elapsedMs)
            )
        )
    }

    private fun executeShizukuStatus(request: BridgeProtocol.BridgeRequest): CommandResponse {
        val engine = ShizukuEngine.getInstance(context)
        val available = engine.isServiceAvailable()
        val hasPermission = engine.hasPermission()
        
        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put("available", available)
                    .put("has_permission", hasPermission)
            )
        )
    }

    private fun executeShizukuShell(request: BridgeProtocol.BridgeRequest): CommandResponse {
        val command = request.args.optString("command", "")
        if (command.isBlank()) {
            return CommandResponse.failure(400, BridgeProtocol.error(request.requestId, "INVALID_ARGS", "command is required"))
        }

        val engine = ShizukuEngine.getInstance(context)
        val result = engine.execShell(command)

        if (!result.success) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(request.requestId, result.errorCode ?: "SHIZUKU_ERROR", result.message ?: "Execution failed")
            )
        }

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put("operation_id", result.operationId)
                    .put("exit_code", result.exitCode)
                    .put("output", result.output)
                    .put("error", result.error)
                    .put("elapsed_ms", result.elapsedMs)
            )
        )
    }

    private fun executeGhostStart(request: BridgeProtocol.BridgeRequest): CommandResponse {
        val engine = GhostEngine.getInstance(context)

        // If a projection token was already captured via the UI button, reuse it.
        val projectionData = GhostProjectionHolder.data
        val projectionResult = GhostProjectionHolder.resultCode

        val displayId = if (projectionData != null && projectionResult == android.app.Activity.RESULT_OK) {
            engine.createGhostDisplay(resultCode = projectionResult, data = projectionData)
        } else {
            engine.createGhostDisplay()
        }

        return if (displayId != -1) {
            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject().put("display_id", displayId).put("status", "ACTIVE")
                )
            )
        } else {
            CommandResponse.failure(
                500,
                BridgeProtocol.error(
                    request.requestId,
                    "GHOST_INIT_FAILED",
                    "Could not create virtual display. Grant Ghost Mode permission in the app first."
                )
            )
        }
    }

    private fun executeGhostStop(request: BridgeProtocol.BridgeRequest): CommandResponse {
        GhostEngine.getInstance(context).releaseGhostDisplay()
        return CommandResponse.success(
            BridgeProtocol.success(request.requestId, JSONObject().put("status", "STOPPED"))
        )
    }

    private fun executeGhostScreenshot(request: BridgeProtocol.BridgeRequest): CommandResponse {
        val engine = GhostEngine.getInstance(context)

        if (!engine.isGhostModeActive()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(request.requestId, "GHOST_NOT_ACTIVE", "Ghost mode is not active")
            )
        }

        val quality = request.args.optInt("quality", 80)

        return try {
            val jpegBytes = engine.captureGhostDisplay(quality)

            if (jpegBytes == null) {
                return CommandResponse.failure(
                    500,
                    BridgeProtocol.error(request.requestId, "GHOST_CAPTURE_FAILED", "No frame available yet")
                )
            }

            val base64 = Base64.encodeToString(jpegBytes, Base64.NO_WRAP)

            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put("base64", base64)
                        .put("byte_count", jpegBytes.size)
                        .put("display_id", engine.getGhostDisplayId())
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "ghost_screenshot failed", exception)
            CommandResponse.failure(
                500,
                BridgeProtocol.error(request.requestId, "GHOST_CAPTURE_FAILED", exception.message ?: "Capture error")
            )
        }
    }

    private fun executeTap(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        if (!request.args.has("x") ||
            !request.args.has("y")
        ) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_COORDINATES",
                    "tap requires x and y"
                )
            )
        }

        val x = request.args.optDouble("x", Double.NaN)
        val y = request.args.optDouble("y", Double.NaN)

        if (!x.isFinite() || !y.isFinite()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "INVALID_COORDINATES",
                    "x and y must be finite numbers"
                )
            )
        }

        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
            ?: return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future: Future<AgentAccessibilityService.GestureResult> =
            executor.submit<AgentAccessibilityService.GestureResult> {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result: AgentAccessibilityService.GestureResult? =
                    null

                accessibility.tap(
                    x.toFloat(),
                    y.toFloat()
                ) {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.GestureResult(
                        false,
                        "GESTURE_TIMEOUT",
                        "Gesture confirmation timed out",
                        null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.GestureResult(
                            false,
                            "GESTURE_NO_RESULT",
                            "Gesture returned no result",
                            null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return if (result.success) {
            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put(
                            "operation_id",
                            result.operationId
                        )
                        .put("gesture_completed", true)
                )
            )
        } else {
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.errorCode
                        ?: "GESTURE_FAILED",
                    result.errorMessage
                        ?: "Gesture failed"
                )
            )
        }
    }

    private fun executeBack(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val lock = java.util.concurrent.CountDownLatch(1)

        var result: AgentAccessibilityService.GestureResult? =
            null

        accessibility.back {
            result = it
            lock.countDown()
        }

        val completed = lock.await(
            GESTURE_TIMEOUT_MS,
            TimeUnit.MILLISECONDS
        )

        if (!completed) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "BACK_TIMEOUT",
                    "Back action confirmation timed out"
                )
            )
        }

        val finalResult = result

        return if (finalResult?.success == true) {
            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put(
                            "operation_id",
                            finalResult.operationId
                        )
                        .put("dispatched", true)
                )
            )
        } else {
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    finalResult?.errorCode
                        ?: "BACK_FAILED",
                    finalResult?.errorMessage
                        ?: "Back action failed"
                )
            )
        }
    }

    private fun executeInputText(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val text = request.args.optString("text", "")

        if (text.isEmpty()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_TEXT",
                    "input_text requires a non-empty text"
                )
            )
        }

        return try {
            val accessibility = AgentAccessibilityService.getInstance()

            if (accessibility == null || !accessibility.isConnected()) {
                return CommandResponse.failure(
                    503,
                    BridgeProtocol.error(
                        request.requestId,
                        "ACCESSIBILITY_NOT_CONNECTED",
                        "Accessibility service is not connected"
                    )
                )
            }

            val executor = commandExecutor
                ?: return CommandResponse.failure(
                    503,
                    BridgeProtocol.error(
                        request.requestId,
                        "COMMAND_EXECUTOR_UNAVAILABLE",
                        "Bridge command executor is unavailable"
                    )
                )

            if (executor.isShutdown) {
                return CommandResponse.failure(
                    503,
                    BridgeProtocol.error(
                        request.requestId,
                        "COMMAND_EXECUTOR_STOPPED",
                        "Bridge command executor is stopped"
                    )
                )
            }

            val future: Future<AgentAccessibilityService.GestureResult> =
                executor.submit<
                    AgentAccessibilityService.GestureResult
                > {
                    val lock = java.util.concurrent.CountDownLatch(1)

                    var result: AgentAccessibilityService.GestureResult? =
                        null

                    accessibility.inputText(text) {
                        result = it
                        lock.countDown()
                    }

                    if (!lock.await(
                            GESTURE_TIMEOUT_MS,
                            TimeUnit.MILLISECONDS
                        )
                    ) {
                        AgentAccessibilityService.GestureResult(
                            false,
                            "INPUT_TIMEOUT",
                            "Input confirmation timed out",
                            null
                        )
                    } else {
                        result
                            ?: AgentAccessibilityService.GestureResult(
                                false,
                                "INPUT_NO_RESULT",
                                "Input returned no result",
                                null
                            )
                    }
                }

            val result = try {
                future.get(
                    GESTURE_TIMEOUT_MS + 1_000,
                    TimeUnit.MILLISECONDS
                )
            } catch (timeoutException: java.util.concurrent.TimeoutException) {
                Log.e(
                    TAG,
                    "input_text future timed out",
                    timeoutException
                )
                AgentAccessibilityService.GestureResult(
                    false,
                    "INPUT_TIMEOUT",
                    "Input task did not complete in time",
                    null
                )
            } catch (interruptedException: InterruptedException) {
                Thread.currentThread().interrupt()
                AgentAccessibilityService.GestureResult(
                    false,
                    "INPUT_INTERRUPTED",
                    "Input task was interrupted",
                    null
                )
            } catch (executionException: java.util.concurrent.ExecutionException) {
                Log.e(TAG, "input_text task failed", executionException)
                AgentAccessibilityService.GestureResult(
                    false,
                    "INPUT_TASK_FAILED",
                    executionException.cause?.message
                        ?: "Input task failed unexpectedly",
                    null
                )
            }

            gestureResultToResponse(request, result, "INPUT_FAILED")
        } catch (exception: Exception) {
            Log.e(TAG, "executeInputText failed", exception)
            CommandResponse.failure(
                500,
                BridgeProtocol.error(
                    request.requestId,
                    "INPUT_UNEXPECTED",
                    exception.message ?: "Unexpected input failure"
                )
            )
        }
    }

    private fun executeSwipe(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        if (!request.args.has("x1") || !request.args.has("y1") ||
            !request.args.has("x2") || !request.args.has("y2")
        ) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_COORDINATES",
                    "swipe requires x1, y1, x2, y2"
                )
            )
        }

        val x1 = request.args.optDouble("x1", Double.NaN)
        val y1 = request.args.optDouble("y1", Double.NaN)
        val x2 = request.args.optDouble("x2", Double.NaN)
        val y2 = request.args.optDouble("y2", Double.NaN)

        if (!x1.isFinite() || !y1.isFinite() ||
            !x2.isFinite() || !y2.isFinite()
        ) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "INVALID_COORDINATES",
                    "swipe coordinates must be finite numbers"
                )
            )
        }

        val durationMs = request.args.optLong("duration_ms", 300L)
            .coerceIn(1L, 60_000L)

        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
            ?: return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future: Future<AgentAccessibilityService.GestureResult> =
            executor.submit<AgentAccessibilityService.GestureResult> {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result: AgentAccessibilityService.GestureResult? =
                    null

                accessibility.swipe(
                    x1.toFloat(),
                    y1.toFloat(),
                    x2.toFloat(),
                    y2.toFloat(),
                    durationMs
                ) {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.GestureResult(
                        false,
                        "GESTURE_TIMEOUT",
                        "Swipe confirmation timed out",
                        null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.GestureResult(
                            false,
                            "GESTURE_NO_RESULT",
                            "Swipe returned no result",
                            null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return gestureResultToResponse(request, result, "SWIPE_FAILED")
    }

    private fun executeLongPress(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        if (!request.args.has("x") ||
            !request.args.has("y")
        ) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_COORDINATES",
                    "long_press requires x and y"
                )
            )
        }

        val x = request.args.optDouble("x", Double.NaN)
        val y = request.args.optDouble("y", Double.NaN)

        if (!x.isFinite() || !y.isFinite()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "INVALID_COORDINATES",
                    "x and y must be finite numbers"
                )
            )
        }

        val durationMs = request.args.optLong("duration_ms", 600L)
            .coerceIn(100L, 10_000L)

        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
            ?: return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future: Future<AgentAccessibilityService.GestureResult> =
            executor.submit<AgentAccessibilityService.GestureResult> {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result: AgentAccessibilityService.GestureResult? =
                    null

                accessibility.longPress(
                    x.toFloat(),
                    y.toFloat(),
                    durationMs
                ) {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.GestureResult(
                        false,
                        "GESTURE_TIMEOUT",
                        "Long press confirmation timed out",
                        null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.GestureResult(
                            false,
                            "GESTURE_NO_RESULT",
                            "Long press returned no result",
                            null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return gestureResultToResponse(
            request, result, "LONG_PRESS_FAILED"
        )
    }

    private fun executeClearText(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
            ?: return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future: Future<AgentAccessibilityService.GestureResult> =
            executor.submit<AgentAccessibilityService.GestureResult> {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result: AgentAccessibilityService.GestureResult? =
                    null

                accessibility.clearText {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.GestureResult(
                        false,
                        "CLEAR_TIMEOUT",
                        "Clear confirmation timed out",
                        null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.GestureResult(
                            false,
                            "CLEAR_NO_RESULT",
                            "Clear returned no result",
                            null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return gestureResultToResponse(request, result, "CLEAR_FAILED")
    }

    private fun executeEraseText(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility = AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
        if (executor == null || executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )
        }

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future: Future<AgentAccessibilityService.GestureResult> =
            executor.submit<AgentAccessibilityService.GestureResult> {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result: AgentAccessibilityService.GestureResult? =
                    null

                accessibility.eraseText {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.GestureResult(
                        false,
                        "ERASE_TIMEOUT",
                        "Erase confirmation timed out",
                        null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.GestureResult(
                            false,
                            "ERASE_NO_RESULT",
                            "Erase returned no result",
                            null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return gestureResultToResponse(request, result, "ERASE_FAILED")
    }

    private fun executeWindowInfo(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val info = accessibility.windowInfo()

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject()
                    .put(
                        "package_name",
                        info.packageName ?: JSONObject.NULL
                    )
                    .put(
                        "activity_name",
                        info.activityName ?: JSONObject.NULL
                    )
                    .put(
                        "window_title",
                        info.windowTitle ?: JSONObject.NULL
                    )
            )
        )
    }

    private fun executeListWindows(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val result = accessibility.listWindows()

        if (!result.success) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.errorCode ?: "LIST_WINDOWS_FAILED",
                    result.errorMessage ?: "Unable to list windows"
                )
            )
        }

        val windows = JSONArray()

        for (entry in result.windows) {
            val bounds = JSONObject()
                .put("left", entry.boundsLeft)
                .put("top", entry.boundsTop)
                .put("right", entry.boundsRight)
                .put("bottom", entry.boundsBottom)

            windows.put(
                JSONObject()
                    .put("id", entry.id)
                    .putNullable("package_name", entry.packageName)
                    .putNullable("title", entry.title)
                    .put("active", entry.active)
                    .put("focused", entry.focused)
                    .put("window_type", entry.windowType)
                    .put("bounds", bounds)
                    .put("root_child_count", entry.rootChildCount)
            )
        }

        return CommandResponse.success(
            BridgeProtocol.success(
                request.requestId,
                JSONObject().put("windows", windows)
            )
        )
    }

    private fun executeNodeAction(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val action = request.args.optString("action", "").trim()

        if (action.isEmpty()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_NODE_ACTION",
                    "node_action requires an action"
                )
            )
        }

        val targetPackage =
            request.args.optString("package", "").trim()
                .ifEmpty { null }

        val resourceId =
            request.args.optString("resource_id", "").trim()
                .ifEmpty { null }

        val text = request.args.optString("text", "").trim()
            .ifEmpty { null }

        val contentDescription =
            request.args.optString("content_description", "").trim()
                .ifEmpty { null }

        val className =
            request.args.optString("class_name", "").trim()
                .ifEmpty { null }

        val matchIndex = request.args.optInt("match_index", 0)
        val argumentText =
            request.args.optString("text_argument", "").trim()
                .ifEmpty { null }

        val nodePath: IntArray? = if (request.args.has("node_path")) {
            try {
                val pathArray = request.args.getJSONArray("node_path")
                val path = IntArray(pathArray.length())

                for (pathIndex in 0 until pathArray.length()) {
                    path[pathIndex] = pathArray.getInt(pathIndex)
                }

                if (path.isEmpty()) null else path
            } catch (exception: Exception) {
                return CommandResponse.failure(
                    400,
                    BridgeProtocol.error(
                        request.requestId,
                        "INVALID_NODE_PATH",
                        "node_path must be an array of child indices"
                    )
                )
            }
        } else {
            null
        }

        val hasSelector = !resourceId.isNullOrEmpty() ||
            !text.isNullOrEmpty() ||
            !contentDescription.isNullOrEmpty() ||
            !className.isNullOrEmpty()

        if (!hasSelector && nodePath == null) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_NODE_SELECTOR",
                    "node_action needs a node_path or at least one of " +
                        "resource_id, text, content_description " +
                        "or class_name"
                )
            )
        }

        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val executor = commandExecutor
            ?: return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_UNAVAILABLE",
                    "Bridge command executor is unavailable"
                )
            )

        if (executor.isShutdown) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "COMMAND_EXECUTOR_STOPPED",
                    "Bridge command executor is stopped"
                )
            )
        }

        val future:
            Future<AgentAccessibilityService.NodeActionResult> =
            executor.submit<
                AgentAccessibilityService.NodeActionResult
                > {
                val lock = java.util.concurrent.CountDownLatch(1)

                var result:
                    AgentAccessibilityService.NodeActionResult? = null

                accessibility.nodeAction(
                    action = action,
                    targetPackage = targetPackage,
                    resourceId = resourceId,
                    text = text,
                    contentDescription = contentDescription,
                    className = className,
                    matchIndex = matchIndex,
                    argumentText = argumentText,
                    nodePath = nodePath
                ) {
                    result = it
                    lock.countDown()
                }

                if (!lock.await(
                        GESTURE_TIMEOUT_MS,
                        TimeUnit.MILLISECONDS
                    )
                ) {
                    AgentAccessibilityService.NodeActionResult(
                        success = false,
                        errorCode = "NODE_ACTION_TIMEOUT",
                        errorMessage =
                            "Node action confirmation timed out",
                        operationId = null,
                        matched = null
                    )
                } else {
                    result
                        ?: AgentAccessibilityService.NodeActionResult(
                            success = false,
                            errorCode = "NODE_ACTION_NO_RESULT",
                            errorMessage = "Node action returned no result",
                            operationId = null,
                            matched = null
                        )
                }
            }

        val result = future.get(
            GESTURE_TIMEOUT_MS + 1_000,
            TimeUnit.MILLISECONDS
        )

        return if (result.success) {
            val matched = result.matched

            val data = JSONObject()
                .put("operation_id", result.operationId)
                .put("action_performed", true)

            if (matched != null) {
                data.put(
                    "matched",
                    JSONObject()
                        .putNullable("package_name", matched.packageName)
                        .putNullable("resource_id", matched.resourceId)
                        .putNullable("text", matched.text)
                        .putNullable(
                            "content_description",
                            matched.contentDescription
                        )
                        .putNullable("class_name", matched.className)
                        .put("match_index", matched.matchIndex)
                        .put("match_count", matched.matchCount)
                        .put(
                            "bounds",
                            JSONObject()
                                .put("left", matched.boundsLeft)
                                .put("top", matched.boundsTop)
                                .put("right", matched.boundsRight)
                                .put("bottom", matched.boundsBottom)
                        )
                )
            }

            CommandResponse.success(
                BridgeProtocol.success(request.requestId, data)
            )
        } else {
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.errorCode ?: "NODE_ACTION_FAILED",
                    result.errorMessage ?: "Node action failed"
                )
            )
        }
    }

    private fun executeKeyEvent(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val keycode = request.args.optString("keycode", "").trim()

        if (keycode.isEmpty()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_KEYCODE",
                    "key_event requires a non-empty keycode"
                )
            )
        }

        val accessibility =
            AgentAccessibilityService.getInstance()

        if (accessibility == null ||
            !accessibility.isConnected()
        ) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "ACCESSIBILITY_NOT_CONNECTED",
                    "Accessibility service is not connected"
                )
            )
        }

        val lock = java.util.concurrent.CountDownLatch(1)

        var result: AgentAccessibilityService.GestureResult? =
            null

        accessibility.keyEvent(keycode) {
            result = it
            lock.countDown()
        }

        val completed = lock.await(
            GESTURE_TIMEOUT_MS,
            TimeUnit.MILLISECONDS
        )

        if (!completed) {
            return CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "KEY_EVENT_TIMEOUT",
                    "Key event confirmation timed out"
                )
            )
        }

        val finalResult = result

        return gestureResultToResponse(
            request,
            finalResult
                ?: AgentAccessibilityService.GestureResult(
                    false,
                    "KEY_EVENT_NO_RESULT",
                    "Key event returned no result",
                    null
                ),
            "KEY_EVENT_FAILED"
        )
    }

    private fun executeOpenUrl(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val url = request.args.optString("url", "").trim()

        if (url.isEmpty()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_URL",
                    "open_url requires a non-empty url"
                )
            )
        }

        val intent = if (url.startsWith("data:", ignoreCase = true)) {
            buildDataUrlIntent(url)
        } else {
            try {
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
            } catch (exception: Exception) {
                Log.e(TAG, "Failed to parse url", exception)
                null
            }
        } ?: return CommandResponse.failure(
            400,
            BridgeProtocol.error(
                request.requestId,
                "INVALID_URL",
                "Could not parse the url"
            )
        )

        return try {
            context.startActivity(intent)
            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put("dispatched", true)
                        .put("url", url)
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "open_url failed", exception)
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "OPEN_URL_FAILED",
                    exception.message ?: "Could not open url"
                )
            )
        }
    }

    private fun executeLaunchApp(
        request: BridgeProtocol.BridgeRequest
    ): CommandResponse {
        val pkg = request.args.optString("package", "").trim()

        if (pkg.isEmpty()) {
            return CommandResponse.failure(
                400,
                BridgeProtocol.error(
                    request.requestId,
                    "MISSING_PACKAGE",
                    "launch_app requires a non-empty package"
                )
            )
        }

        val ghostEngine = GhostEngine.getInstance(context)
        val shizukuEngine = ShizukuEngine.getInstance(context)

        if (ghostEngine.isGhostModeActive() && shizukuEngine.hasPermission()) {
            val displayId = ghostEngine.getGhostDisplayId()
            val cmd = "am start --display $displayId $pkg"
            val res = shizukuEngine.execShell(cmd)
            
            if (res.success) {
                return CommandResponse.success(
                    BridgeProtocol.success(request.requestId, JSONObject().put("launched_on_ghost", true).put("display_id", displayId))
                )
            }
        }

        return try {
            val intent =
                context.packageManager.getLaunchIntentForPackage(pkg)
                    ?: return CommandResponse.failure(
                        404,
                        BridgeProtocol.error(
                            request.requestId,
                            "PACKAGE_NOT_FOUND",
                            "No launchable activity for package $pkg"
                        )
                    )

            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)

            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put("dispatched", true)
                        .put("package", pkg)
                )
            )
        } catch (exception: Exception) {
            Log.e(TAG, "launch_app failed", exception)
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    "LAUNCH_APP_FAILED",
                    exception.message ?: "Could not launch app"
                )
            )
        }
    }

    private fun buildDataUrlIntent(url: String): Intent? {
        val comma = url.indexOf(',')

        if (comma < 0) {
            return null
        }

        val meta = url.substring(4, comma).lowercase()
        val payload = url.substring(comma + 1)
        val isBase64 = meta.contains(";base64")

        val html = if (isBase64) {
            try {
                String(
                    Base64.decode(payload, Base64.DEFAULT),
                    StandardCharsets.UTF_8
                )
            } catch (exception: IllegalArgumentException) {
                Log.e(TAG, "data: url base64 decode failed", exception)
                return null
            }
        } else {
            try {
                URLDecoder.decode(payload, "UTF-8")
            } catch (exception: Exception) {
                Log.e(TAG, "data: url decode failed", exception)
                return null
            }
        }

        return try {
            val file = File(context.cacheDir, "agentpro_app.html")
            file.writeText(html, StandardCharsets.UTF_8)

            val authority = context.packageName + ".fileprovider"
            val uri = FileProvider.getUriForFile(
                context,
                authority,
                file
            )

            Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "text/html")
                addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            }
        } catch (exception: Exception) {
            Log.e(TAG, "Failed to build data-url intent", exception)
            null
        }
    }

    private fun gestureResultToResponse(
        request: BridgeProtocol.BridgeRequest,
        result: AgentAccessibilityService.GestureResult,
        fallbackCode: String
    ): CommandResponse {
        return if (result.success) {
            CommandResponse.success(
                BridgeProtocol.success(
                    request.requestId,
                    JSONObject()
                        .put("operation_id", result.operationId)
                        .put("gesture_completed", true)
                )
            )
        } else {
            CommandResponse.failure(
                503,
                BridgeProtocol.error(
                    request.requestId,
                    result.errorCode ?: fallbackCode,
                    result.errorMessage ?: "Action failed"
                )
            )
        }
    }

    private fun readHttpRequest(
        socket: Socket
    ): HttpRequest {
        val input = socket.getInputStream()

        var totalHeaderCharacters = 0

        fun readLineBytes(): ByteArray? {
            val buffer = java.io.ByteArrayOutputStream()
            var lineBytes = 0

            while (true) {
                val current = input.read()

                if (current == -1) {
                    if (lineBytes == 0) {
                        return null
                    }
                    break
                }

                lineBytes += 1

                totalHeaderCharacters += 1

                if (totalHeaderCharacters > MAX_HEADER_BYTES) {
                    throw IOException("HTTP headers are too large")
                }

                if (current == '\n'.code) {
                    break
                }

                buffer.write(current)
            }

            return buffer.toByteArray()
        }

        val requestLineBytes = readLineBytes()
            ?: throw IOException("Missing HTTP request line")

        val requestLine = String(
            requestLineBytes,
            StandardCharsets.UTF_8
        ).trimEnd('\r')

        val requestParts = requestLine.split(" ")

        if (requestParts.size != 3) {
            throw IOException("Malformed HTTP request line")
        }

        val method = requestParts[0]
        val path = requestParts[1]
        val version = requestParts[2]

        if (version != "HTTP/1.1" &&
            version != "HTTP/1.0"
        ) {
            throw IOException("Unsupported HTTP version")
        }

        var contentLength = -1
        var authorizationToken = ""
        var requestId = ""

        while (true) {
            val lineBytes = readLineBytes()
                ?: throw IOException("Unexpected end of HTTP headers")

            val line = String(
                lineBytes,
                StandardCharsets.UTF_8
            ).trimEnd('\r')

            if (line.isEmpty()) {
                break
            }

            val separator = line.indexOf(':')

            if (separator <= 0) {
                throw IOException("Malformed HTTP header")
            }

            val name = line.substring(
                0,
                separator
            ).trim().lowercase()

            val value = line.substring(
                separator + 1
            ).trim()

            when (name) {
                "content-length" -> {
                    contentLength = value.toIntOrNull()
                        ?: throw IOException(
                            "Invalid Content-Length"
                        )
                }

                "authorization" -> {
                    authorizationToken =
                        parseBearerToken(value)
                }

                "x-request-id" -> {
                    requestId = value
                }
            }
        }

        if (contentLength < 0 ||
            contentLength > MAX_BODY_BYTES
        ) {
            throw IOException(
                "Request body exceeds allowed size"
            )
        }

        val bodyBytes = ByteArray(contentLength)

        var offset = 0

        while (offset < contentLength) {
            val read = input.read(
                bodyBytes,
                offset,
                contentLength - offset
            )

            if (read < 0) {
                throw IOException(
                    "Unexpected end of HTTP request body"
                )
            }

            offset += read
        }

        return HttpRequest(
            method = method,
            path = path,
            body = String(
                bodyBytes,
                StandardCharsets.UTF_8
            ),
            authorizationToken = authorizationToken,
            requestId = requestId
        )
    }

    private fun parseBearerToken(
        value: String
    ): String {
        val prefix = "Bearer "

        return if (value.startsWith(
                prefix,
                ignoreCase = true
            )
        ) {
            value.substring(prefix.length).trim()
        } else {
            ""
        }
    }

    private fun writeHttpResponse(
        socket: Socket,
        statusCode: Int,
        body: JSONObject
    ) {
        val bytes = body
            .toString()
            .toByteArray(StandardCharsets.UTF_8)

        val statusText = when (statusCode) {
            200 -> "OK"
            400 -> "Bad Request"
            401 -> "Unauthorized"
            404 -> "Not Found"
            405 -> "Method Not Allowed"
            500 -> "Internal Server Error"
            503 -> "Service Unavailable"
            else -> "Error"
        }

        val writer = BufferedWriter(
            OutputStreamWriter(
                socket.getOutputStream(),
                StandardCharsets.UTF_8
            )
        )

        writer.write(
            "HTTP/1.1 $statusCode $statusText\r\n"
        )
        writer.write("Content-Type: application/json; charset=utf-8\r\n")
        writer.write("Content-Length: ${bytes.size}\r\n")
        writer.write("Connection: close\r\n")
        writer.write("\r\n")
        writer.flush()

        socket.getOutputStream().write(bytes)
        socket.getOutputStream().flush()
    }

    private fun secureEquals(
        expected: String,
        actual: String
    ): Boolean {
        val expectedBytes =
            expected.toByteArray(StandardCharsets.UTF_8)

        val actualBytes =
            actual.toByteArray(StandardCharsets.UTF_8)

        if (expectedBytes.size != actualBytes.size) {
            return false
        }

        var result = 0

        for (index in expectedBytes.indices) {
            result = result or (
                expectedBytes[index].toInt() xor
                    actualBytes[index].toInt()
                )
        }

        return result == 0
    }

    data class HttpRequest(
        val method: String,
        val path: String,
        val body: String,
        val authorizationToken: String,
        val requestId: String
    )

    private fun JSONObject.putNullable(
        key: String,
        value: String?
    ): JSONObject {
        return if (value == null) {
            put(key, JSONObject.NULL)
        } else {
            put(key, value)
        }
    }

    data class CommandResponse(
        val ok: Boolean,
        val httpCode: Int,
        val json: JSONObject
    ) {
        companion object {
            fun success(
                json: JSONObject
            ): CommandResponse {
                return CommandResponse(
                    true,
                    200,
                    json
                )
            }

            fun failure(
                httpCode: Int,
                json: JSONObject
            ): CommandResponse {
                return CommandResponse(
                    false,
                    httpCode,
                    json
                )
            }
        }
    }
}
